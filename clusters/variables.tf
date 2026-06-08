# =============================================================================
# Shared variables consumed by every auto-generated cluster file.
# These are set via -var flags in CircleCI (TF_VPC_ID, TF_SUBNET_IDS, etc.).
# =============================================================================

variable "vpc_id" {
  description = "VPC ID where all provisioned RDS clusters will be deployed."
  type        = string
}

variable "subnet_ids" {
  description = <<-EOT
    Private subnet IDs for DB subnet groups.
    Pass as a JSON array from CircleCI:
      TF_SUBNET_IDS = ["subnet-aaa","subnet-bbb"]
  EOT
  type        = list(string)
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "At least two subnets in different AZs are required."
  }
}

variable "allowed_security_group_ids" {
  description = <<-EOT
    Security group IDs allowed inbound DB access (e.g. the Lambda execution SG).
    Pass as a JSON array from CircleCI:
      TF_ALLOWED_SGS = ["sg-xxx"]
  EOT
  type    = list(string)
  default = []
}
