data "aws_iot_endpoint" "ats" {
  endpoint_type = "iot:Data-ATS"
}

# ---------------------------------------------------------------- topic rule: MQTT -> Kinesis (+ Firehose archive)
data "aws_iam_policy_document" "iot_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "iot_rule" {
  name               = "${local.name}-iot-rule"
  assume_role_policy = data.aws_iam_policy_document.iot_trust.json
}

resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  name              = "/rpm/${var.environment}/iot-rule-errors"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_iam_role_policy" "iot_rule" {
  role = aws_iam_role.iot_rule.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["kinesis:PutRecord", "kinesis:PutRecords"], Resource = aws_kinesis_stream.telemetry.arn },
      { Effect = "Allow", Action = ["firehose:PutRecord", "firehose:PutRecordBatch"],
      Resource = aws_kinesis_firehose_delivery_stream.lake.arn },
      { Effect = "Allow", Action = ["kms:GenerateDataKey"], Resource = aws_kms_key.main.arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = "${aws_cloudwatch_log_group.iot_rule_errors.arn}:*" },
    ]
  })
}

resource "aws_iot_topic_rule" "ingest" {
  name        = "rpm_${var.environment}_ingest"
  description = "Device telemetry + events -> Kinesis (thing name taken from the topic, never the payload)"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, timestamp() AS ingest_ts_ms, topic(4) AS thing, topic(5) AS kind FROM 'rpm/v1/dev/+/+' WHERE topic(5) = 'telemetry' OR topic(5) = 'events'"

  kinesis {
    role_arn      = aws_iam_role.iot_rule.arn
    stream_name   = aws_kinesis_stream.telemetry.name
    partition_key = "$${topic(4)}"
  }

  firehose {
    role_arn             = aws_iam_role.iot_rule.arn
    delivery_stream_name = aws_kinesis_firehose_delivery_stream.lake.name
    separator            = "\n"
  }

  error_action {
    cloudwatch_logs {
      role_arn       = aws_iam_role.iot_rule.arn
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
    }
  }
}

# ---------------------------------------------------------------- policies (least privilege per device identity)
# Real devices: may only use their own thing name as client id and their own topics / shadow.
resource "aws_iot_policy" "device" {
  name = "${local.name}-device"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "iot:Connect",
      Resource = "${local.iot_arn}:client/$${iot:Connection.Thing.ThingName}" },
      { Effect = "Allow", Action = "iot:Publish", Resource = [
        "${local.iot_arn}:topic/rpm/v1/dev/$${iot:Connection.Thing.ThingName}/telemetry",
        "${local.iot_arn}:topic/rpm/v1/dev/$${iot:Connection.Thing.ThingName}/events",
        "${local.iot_arn}:topic/$aws/things/$${iot:Connection.Thing.ThingName}/shadow/*",
      ] },
      { Effect = "Allow", Action = "iot:Subscribe", Resource = [
        "${local.iot_arn}:topicfilter/rpm/v1/dev/$${iot:Connection.Thing.ThingName}/cmd",
        "${local.iot_arn}:topicfilter/$aws/things/$${iot:Connection.Thing.ThingName}/shadow/*",
      ] },
      { Effect = "Allow", Action = "iot:Receive", Resource = [
        "${local.iot_arn}:topic/rpm/v1/dev/$${iot:Connection.Thing.ThingName}/cmd",
        "${local.iot_arn}:topic/$aws/things/$${iot:Connection.Thing.ThingName}/shadow/*",
      ] },
    ]
  })
}

# Simulator fleet: one certificate publishing for virtual devices sim-*; can never touch real devices' topics.
resource "aws_iot_policy" "sim_fleet" {
  name = "${local.name}-sim-fleet"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "iot:Connect",
      Resource = ["${local.iot_arn}:client/sim-fleet-*", "${local.iot_arn}:client/sim-ctl-*"] },
      { Effect = "Allow", Action = "iot:Publish",
      Resource = ["${local.iot_arn}:topic/rpm/v1/dev/sim-*", "${local.iot_arn}:topic/rpm/v1/sim/control"] },
      { Effect = "Allow", Action = "iot:Subscribe",
      Resource = ["${local.iot_arn}:topicfilter/rpm/v1/dev/+/cmd", "${local.iot_arn}:topicfilter/rpm/v1/sim/control"] },
      { Effect = "Allow", Action = "iot:Receive",
      Resource = ["${local.iot_arn}:topic/rpm/v1/dev/sim-*", "${local.iot_arn}:topic/rpm/v1/sim/control"] },
    ]
  })
}

# ---------------------------------------------------------------- identities
resource "aws_iot_thing_type" "wearable" {
  name = "rpm-wearable-gateway"
  properties {
    description = "Galaxy Watch + Android phone gateway"
  }
}

resource "aws_iot_thing" "demo_watch" {
  name            = var.demo_device_id
  thing_type_name = aws_iot_thing_type.wearable.name
}

# Demo shortcut: AWS-generated keys land in encrypted Terraform state and are written to git-ignored files by
# cloud-up.sh. Production path (week 3): fleet provisioning by claim, private key generated on the device.
resource "aws_iot_certificate" "demo_watch" {
  active = true
}

resource "aws_iot_thing_principal_attachment" "demo_watch" {
  thing     = aws_iot_thing.demo_watch.name
  principal = aws_iot_certificate.demo_watch.arn
}

resource "aws_iot_policy_attachment" "demo_watch" {
  policy = aws_iot_policy.device.name
  target = aws_iot_certificate.demo_watch.arn
}

resource "aws_iot_certificate" "sim_fleet" {
  active = true
}

resource "aws_iot_policy_attachment" "sim_fleet" {
  policy = aws_iot_policy.sim_fleet.name
  target = aws_iot_certificate.sim_fleet.arn
}
