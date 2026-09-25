data "aws_caller_identity" "me" {}
data "aws_partition" "current" {}
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name       = "${var.name}-${var.environment}"
  account_id = data.aws_caller_identity.me.account_id
  partition  = data.aws_partition.current.partition
  azs        = slice(data.aws_availability_zones.available.names, 0, 2)
  namespace  = "rpm" # Kubernetes namespace for platform workloads
  iot_arn    = "arn:${local.partition}:iot:${var.region}:${local.account_id}"
}
