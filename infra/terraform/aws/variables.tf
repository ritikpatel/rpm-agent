variable "region" {
  description = "The project's selected Region. In the new AWS experience every Regional resource must live here."
  type        = string
  default     = "ap-southeast-2"
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
  description = "ON_DEMAND for demo-day reliability. SPOT cuts cost during development but is denied on the Free plan of the new AWS experience."
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
  description = "Run the ICU gateway as a Greengrass v2 component on an EC2 core device (adds ~$0.02/h) instead of as a pod on EKS. Greengrass is not available in the new AWS experience without advanced features: keep false there."
  type        = bool
  default     = false
}

variable "greengrass_instance_type" {
  description = "Graviton instance for the Greengrass core (nucleus JVM + gateway fit in 2 GiB)."
  type        = string
  default     = "t4g.small"
}

variable "console_urls" {
  description = "Origins the clinician console is served from (Cognito callback/logout). Demo: the port-forward."
  type        = list(string)
  default     = ["http://localhost:3000"]
}

variable "audit_lock_mode" {
  description = "S3 Object Lock mode for the audit bucket: GOVERNANCE (demo, removable by cloud-down) or COMPLIANCE."
  type        = string
  default     = "GOVERNANCE"
}

variable "audit_retention_days" {
  description = "Write-once retention for audit records (HIPAA documentation retention is 6 years = 2190 days)."
  type        = number
  default     = 7
}
