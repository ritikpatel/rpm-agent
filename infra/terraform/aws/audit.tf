# Tamper-evident audit trail (HIPAA §164.312(b) audit controls, §164.316(b)(2) retention):
#   - CloudTrail: every AWS API call (who changed infrastructure, who read secrets), log-file integrity validation
#   - Application audit: every PHI read/action from audit_log, exported hourly as FHIR AuditEvent (audit-export job)
# Both land in a write-once bucket (S3 Object Lock). Demo: GOVERNANCE mode with short retention so cloud-down can
# still remove it. Production: COMPLIANCE mode, 6 years, no force_destroy.

resource "aws_s3_bucket" "audit" {
  bucket              = "${local.name}-audit-${local.account_id}"
  object_lock_enabled = true
  force_destroy       = true # demo only (governance-mode locks are bypassed by the destroy)
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = var.audit_lock_mode
      days = var.audit_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.audit]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "audit" {
  bucket = aws_s3_bucket.audit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid      = "DenyInsecureTransport", Effect = "Deny", Principal = "*", Action = "s3:*",
        Resource = [aws_s3_bucket.audit.arn, "${aws_s3_bucket.audit.arn}/*"],
      Condition = { Bool = { "aws:SecureTransport" = "false" } } },
      { Sid    = "CloudTrailAclCheck", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" },
        Action = "s3:GetBucketAcl", Resource = aws_s3_bucket.audit.arn,
      Condition = { StringEquals = { "aws:SourceArn" = local.trail_arn } } },
      { Sid    = "CloudTrailWrite", Effect = "Allow", Principal = { Service = "cloudtrail.amazonaws.com" },
        Action = "s3:PutObject", Resource = "${aws_s3_bucket.audit.arn}/cloudtrail/AWSLogs/${local.account_id}/*",
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control",
      "aws:SourceArn" = local.trail_arn } } },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.audit]
}

locals {
  trail_arn = "arn:${local.partition}:cloudtrail:${var.region}:${local.account_id}:trail/${local.name}"
}

resource "aws_cloudtrail" "main" {
  name                          = local.name
  s3_bucket_name                = aws_s3_bucket.audit.id
  s3_key_prefix                 = "cloudtrail"
  kms_key_id                    = aws_kms_key.main.arn
  enable_log_file_validation    = true # digest files: tampering with a delivered log is detectable
  include_global_service_events = true
  is_multi_region_trail         = false # demo runs in one region
  event_selector {
    read_write_type           = "All"
    include_management_events = true
    data_resource {
      type   = "AWS::S3::Object" # who read or wrote patient-derived data files
      values = ["${aws_s3_bucket.data.arn}/", "${aws_s3_bucket.lake.arn}/"]
    }
  }
  depends_on = [aws_s3_bucket_policy.audit]
}
