# ICU bedside gateway. One IoT thing (icu-gw-01) publishes for the bedside monitors that the registry assigns to it
# (device_assignments.gateway_id); the stream engine rejects anything else it sends. Two ways to run it:
#   container   - the same Python code as a pod on EKS, holding its own mTLS MQTT session (default; no extra cost)
#   greengrass  - an AWS IoT Greengrass v2 core device on EC2 (Graviton) running the component from
#                 edge/icu-gateway/greengrass; the nucleus owns the connection and spools while offline

resource "aws_iot_thing_type" "icu_gateway" {
  name = "rpm-icu-gateway"
  properties {
    description = "ICU bedside gateway: HL7 v2 monitors -> bedside NEWS2 -> AWS IoT"
  }
}

resource "aws_iot_thing" "icu_gateway" {
  name            = var.icu_gateway_id
  thing_type_name = aws_iot_thing_type.icu_gateway.name
}

# Demo shortcut, as for the watch: AWS-generated key in encrypted state. A production core device generates its key
# on the box (or in a TPM/HSM) and is provisioned by claim.
resource "aws_iot_certificate" "icu_gateway" {
  active = true
}

resource "aws_iot_thing_principal_attachment" "icu_gateway" {
  thing     = aws_iot_thing.icu_gateway.name
  principal = aws_iot_certificate.icu_gateway.arn
}

resource "aws_iot_policy_attachment" "icu_gateway" {
  policy = aws_iot_policy.device.name # own telemetry/events topics, own cmd + retained config topics
  target = aws_iot_certificate.icu_gateway.arn
}

# ---------------------------------------------------------------- optional: Greengrass v2 core device on EC2
locals {
  gg = var.enable_greengrass_core ? 1 : 0
}

data "aws_iot_endpoint" "credentials" {
  endpoint_type = "iot:CredentialProvider"
}

data "aws_iam_policy_document" "gg_tes_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["credentials.iot.amazonaws.com"]
    }
  }
}

# Token exchange role: what components on the core may do with AWS credentials (artifact download, logs).
resource "aws_iam_role" "gg_token_exchange" {
  count              = local.gg
  name               = "${local.name}-gg-token-exchange"
  assume_role_policy = data.aws_iam_policy_document.gg_tes_trust.json
}

resource "aws_iam_role_policy" "gg_token_exchange" {
  count = local.gg
  role  = aws_iam_role.gg_token_exchange[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.data.arn}/greengrass/*" },
      { Effect = "Allow", Action = ["s3:GetBucketLocation"], Resource = aws_s3_bucket.data.arn },
      local.kms_use,
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
      "logs:DescribeLogStreams"], Resource = "arn:${local.partition}:logs:${var.region}:${local.account_id}:*" },
    ]
  })
}

resource "aws_iot_role_alias" "gg" {
  count               = local.gg
  alias               = "${local.name}-gg-tes"
  role_arn            = aws_iam_role.gg_token_exchange[0].arn
  credential_duration = 3600
}

# Minimal core-device policy (AWS "minimal IoT policy for Greengrass v2 core devices"), scoped to this thing.
resource "aws_iot_policy" "gg_core" {
  count = local.gg
  name  = "${local.name}-greengrass-core"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "iot:Connect",
      Resource = "${local.iot_arn}:client/$${iot:Connection.Thing.ThingName}*" },
      { Effect = "Allow", Action = ["iot:Publish", "iot:Receive"],
      Resource = "${local.iot_arn}:topic/$aws/things/$${iot:Connection.Thing.ThingName}*/*" },
      { Effect = "Allow", Action = "iot:Subscribe",
      Resource = "${local.iot_arn}:topicfilter/$aws/things/$${iot:Connection.Thing.ThingName}*/*" },
      { Effect = "Allow", Action = ["iot:GetThingShadow", "iot:UpdateThingShadow", "iot:DeleteThingShadow"],
      Resource = "${local.iot_arn}:thing/$${iot:Connection.Thing.ThingName}*" },
      { Effect = "Allow", Action = "iot:AssumeRoleWithCertificate", Resource = aws_iot_role_alias.gg[0].arn },
      { Effect = "Allow", Action = ["greengrass:GetComponentVersionArtifact", "greengrass:ResolveComponentCandidates",
      "greengrass:GetDeploymentConfiguration", "greengrass:ListThingGroupsForCoreDevice"], Resource = "*" },
    ]
  })
}

resource "aws_iot_policy_attachment" "gg_core" {
  count  = local.gg
  policy = aws_iot_policy.gg_core[0].name
  target = aws_iot_certificate.icu_gateway.arn
}

# The core's identity reaches the instance through Secrets Manager (KMS-encrypted), never through user data.
resource "aws_secretsmanager_secret" "gg_identity" {
  count                   = local.gg
  name                    = "${local.name}/greengrass/${var.icu_gateway_id}"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "gg_identity" {
  count     = local.gg
  secret_id = aws_secretsmanager_secret.gg_identity[0].id
  secret_string = jsonencode({
    certificate_pem = aws_iot_certificate.icu_gateway.certificate_pem
    private_key     = aws_iot_certificate.icu_gateway.private_key
  })
}

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "gg_instance" {
  count              = local.gg
  name               = "${local.name}-gg-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "gg_ssm" {
  count      = local.gg
  role       = aws_iam_role.gg_instance[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore" # shell via SSM, no SSH
}

resource "aws_iam_role_policy" "gg_instance" {
  count = local.gg
  role  = aws_iam_role.gg_instance[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.gg_identity[0].arn },
      local.kms_use,
    ]
  })
}

resource "aws_iam_instance_profile" "gg" {
  count = local.gg
  name  = "${local.name}-gg"
  role  = aws_iam_role.gg_instance[0].name
}

resource "aws_security_group" "gg" {
  count       = local.gg
  name        = "${local.name}-gg-core"
  description = "ICU gateway: HL7 MLLP and nurse-station status from inside the VPC only"
  vpc_id      = module.vpc.vpc_id
  ingress {
    description = "HL7 v2 MLLP from bedside monitors (simulated on EKS)"
    from_port   = 2575
    to_port     = 2575
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
  ingress {
    description = "nurse-station status + metrics"
    from_port   = 8081
    to_port     = 8081
    protocol    = "tcp"
    cidr_blocks = [module.vpc.vpc_cidr_block]
  }
  egress {
    description = "IoT Core, Greengrass, S3 (via NAT / endpoint)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_instance" "gg_core" {
  count                  = local.gg
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.greengrass_instance_type
  subnet_id              = module.vpc.private_subnets[0]
  vpc_security_group_ids = [aws_security_group.gg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.gg[0].name
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    volume_size = 16
    encrypted   = true
    kms_key_id  = aws_kms_key.main.arn
  }
  user_data = templatefile("${path.module}/templates/greengrass-core.sh.tftpl", {
    region            = var.region
    thing             = aws_iot_thing.icu_gateway.name
    secret_arn        = aws_secretsmanager_secret.gg_identity[0].arn
    role_alias        = aws_iot_role_alias.gg[0].alias
    iot_data_endpoint = data.aws_iot_endpoint.ats.endpoint_address
    iot_cred_endpoint = data.aws_iot_endpoint.credentials.endpoint_address
  })
  user_data_replace_on_change = true
  tags                        = { Name = "${local.name}-gg-core-${var.icu_gateway_id}" }
  depends_on                  = [aws_secretsmanager_secret_version.gg_identity, aws_iot_policy_attachment.gg_core]
}
