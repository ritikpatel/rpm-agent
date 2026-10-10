# ---------------------------------------------------------------- S3: dataset bucket + raw telemetry lake
resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-data-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket" "lake" {
  bucket        = "${local.name}-lake-${local.account_id}"
  force_destroy = true
}

locals {
  buckets = { data = aws_s3_bucket.data, lake = aws_s3_bucket.lake }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "b" {
  for_each = local.buckets
  bucket   = each.value.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "b" {
  for_each                = local.buckets
  bucket                  = each.value.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "tls_only" {
  for_each = local.buckets
  bucket   = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [each.value.arn, "${each.value.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.b]
}

resource "aws_s3_bucket_lifecycle_configuration" "lake" {
  bucket = aws_s3_bucket.lake.id
  rule {
    id     = "expire-demo-telemetry"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

# ---------------------------------------------------------------- SQS: device ingest fan-out, agent work queue,
# alert intake (each with a dead-letter queue)
#
# The IoT rule writes every device message to two queues, so the real-time path and the raw archive fail
# independently (docs/adr/0014):
#   rpm-telemetry          -> stream-engine (NEWS2, rules, alerts)
#   rpm-telemetry-archive  -> telemetry-archiver -> gzip NDJSON in the S3 lake
# Standard queues: the IoT rule's SQS action does not support FIFO queues, and the stream engine orders by event
# time and de-duplicates on msg_id.
locals {
  queues = {
    "rpm-telemetry"         = { visibility = 60, max_receive = 10 }
    "rpm-telemetry-archive" = { visibility = 300, max_receive = 10 } # > archiver flush interval (60 s) + PUT
    "rpm-clinical-events"   = { visibility = 120, max_receive = 5 }
    "rpm-alert-intake"      = { visibility = 30, max_receive = 5 }
  }
}

resource "aws_sqs_queue" "dlq" {
  for_each                  = local.queues
  name                      = "${each.key}-dlq"
  message_retention_seconds = 1209600
  kms_master_key_id         = aws_kms_key.main.arn
}

resource "aws_sqs_queue" "q" {
  for_each                   = local.queues
  name                       = each.key
  visibility_timeout_seconds = each.value.visibility
  receive_wait_time_seconds  = 20
  message_retention_seconds  = 345600 # 4 days of buffering if a consumer is down
  kms_master_key_id          = aws_kms_key.main.arn
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.key].arn
    maxReceiveCount     = each.value.max_receive
  })
}
