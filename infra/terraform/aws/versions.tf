terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Partial config; values supplied by infra/scripts/cloud-up.sh (-backend-config=...)
  backend "s3" {}
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      project     = "rpm"
      environment = var.environment
      managed_by  = "terraform"
      data_class  = "synthetic-phi" # HIPAA-aligned controls applied as if the data were real PHI
    }
  }
}
