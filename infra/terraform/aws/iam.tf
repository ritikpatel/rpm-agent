# EKS Pod Identity: each workload's Kubernetes service account maps to its own least-privilege IAM role.
data "aws_iam_policy_document" "pod_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

locals {
  kms_use = { Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = aws_kms_key.main.arn }
  titan_invoke = { Effect = "Allow", Action = ["bedrock:InvokeModel"],
  Resource = "arn:${local.partition}:bedrock:${var.region}::foundation-model/amazon.titan-embed-text-v2:0" }
  cmd_publish = { Effect = "Allow", Action = ["iot:Publish"], Resource = "${local.iot_arn}:topic/rpm/v1/dev/*/cmd" }
  data_read = [
    { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.data.arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.data.arn}/*" },
  ]
  sqs_consume = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueUrl",
  "sqs:GetQueueAttributes"]
  workloads = {
    "stream-engine" = [
      { Effect = "Allow", Action = local.sqs_consume, Resource = aws_sqs_queue.q["rpm-telemetry"].arn },
      # depth of the dead-letter queue is exported as a metric: anything there was not processed
      { Effect = "Allow", Action = ["sqs:GetQueueUrl", "sqs:GetQueueAttributes"],
      Resource = aws_sqs_queue.dlq["rpm-telemetry"].arn },
      { Effect = "Allow", Action = ["sqs:SendMessage", "sqs:GetQueueUrl"],
      Resource = [aws_sqs_queue.q["rpm-clinical-events"].arn, aws_sqs_queue.q["rpm-alert-intake"].arn] },
      local.kms_use,
    ]
    "telemetry-archiver" = [
      { Effect = "Allow", Action = local.sqs_consume, Resource = aws_sqs_queue.q["rpm-telemetry-archive"].arn },
      # write-only on the raw telemetry prefix of the lake
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.lake.arn}/telemetry/*" },
      local.kms_use,
    ]
    "simulator" = concat(local.data_read, [local.kms_use])
    "fhir-loader" = concat(local.data_read, [local.kms_use,
      # retained per-bed config for ICU gateways (rpm.gateway_config.v1); RetainPublish is a separate action
      { Effect = "Allow", Action = ["iot:Publish", "iot:RetainPublish"],
      Resource = "${local.iot_arn}:topic/rpm/v1/dev/*/config" },
    ])
    "model-serving" = concat(local.data_read, [local.kms_use])
    "rag-index"     = concat(local.data_read, [local.kms_use, local.titan_invoke])
    "agent-service" = concat([local.kms_use, local.titan_invoke, local.cmd_publish,
      { Effect = "Allow", Action = ["bedrock-mantle:CreateInference"], Resource = "*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl"], Resource = aws_sqs_queue.q["rpm-clinical-events"].arn },
    ])
    "alert-service" = [local.kms_use, local.cmd_publish,
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility",
      "sqs:GetQueueUrl"], Resource = aws_sqs_queue.q["rpm-alert-intake"].arn },
      { Effect = "Allow", Action = ["ses:SendEmail"], Resource = "*" },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = "*" }, # SMS to phone numbers (no topic ARN)
    ]
    "audit-export" = [local.kms_use,
      # write-once FHIR AuditEvent batches; no read, delete or retention-bypass rights
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.audit.arn}/app-audit/*" },
    ]
    "api" = [
      # cloud -> device commands (used from week 2 by the agents / console)
      { Effect = "Allow", Action = ["iot:Publish"], Resource = "${local.iot_arn}:topic/rpm/v1/dev/*/cmd" },
      { Effect = "Allow", Action = ["iot:GetThingShadow", "iot:UpdateThingShadow"], Resource = "${local.iot_arn}:thing/*" },
      { Effect = "Allow", Action = ["iot:Publish"], Resource = "${local.iot_arn}:topic/rpm/v1/sim/control" },
    ]
  }
}

resource "aws_iam_role" "workload" {
  for_each           = local.workloads
  name               = "${local.name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_trust.json
}

resource "aws_iam_role_policy" "workload" {
  for_each = local.workloads
  role     = aws_iam_role.workload[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value })
}

resource "aws_eks_pod_identity_association" "workload" {
  for_each        = local.workloads
  cluster_name    = module.eks.cluster_name
  namespace       = local.namespace
  service_account = each.key
  role_arn        = aws_iam_role.workload[each.key].arn
}
