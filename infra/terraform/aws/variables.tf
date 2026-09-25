variable "region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "demo"
}

variable "name" {
  type    = string
  default = "rpm"
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

variable "node_instance_types" {
  description = "Graviton (arm64) nodes: images build natively on Apple-silicon laptops and cost ~20% less."
  type        = list(string)
  default     = ["t4g.large"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND for demo-day reliability; SPOT to cut cost during development."
  type        = string
  default     = "ON_DEMAND"
}

variable "node_desired_size" {
  type    = number
  default = 2
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the EKS public API endpoint. Narrow to your office/home IP for demos."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.small"
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

variable "kinesis_shards" {
  type    = number
  default = 1
}

variable "demo_device_id" {
  description = "IoT thing name of the real Galaxy Watch / phone gateway used in the live demo."
  type        = string
  default     = "gw-demo-01"
}

variable "budget_limit_usd" {
  type    = number
  default = 150
}

variable "budget_email" {
  description = "Email for budget alerts (empty = no budget created)."
  type        = string
  default     = ""
}

variable "alert_sender_email" {
  description = "SES sender identity for escalation emails (verify it from the SES email; sandbox also needs verified recipients). Empty = email via log only."
  type        = string
  default     = ""
}

variable "icu_gateway_id" {
  description = "IoT thing name of the ICU bedside gateway (also the Greengrass core device name)."
  type        = string
  default     = "icu-gw-01"
}

variable "enable_greengrass_core" {
  description = "Run the ICU gateway as a Greengrass v2 component on an EC2 core device (adds ~$0.02/h) instead of as a pod on EKS."
  type        = bool
  default     = false
}

variable "greengrass_instance_type" {
  description = "Graviton instance for the Greengrass core (nucleus JVM + gateway fit in 2 GiB)."
  type        = string
  default     = "t4g.small"
}
