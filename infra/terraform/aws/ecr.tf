locals {
  images = ["stream-engine", "api", "simulator", "fhir-loader", "db-migrate", "model-serving", "alert-service",
  "agent-service", "rag", "console", "icu-gateway"]
}

resource "aws_ecr_repository" "img" {
  for_each             = toset(local.images)
  name                 = "rpm/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_lifecycle_policy" "img" {
  for_each   = aws_ecr_repository.img
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
