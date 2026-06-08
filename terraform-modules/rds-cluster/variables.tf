variable "environment" {
  description = "Deployment environment. Controls instance size, Multi-AZ, and deletion protection."
  type        = string
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "Must be 'dev' or 'prod'."
  }
}

variable "cluster_identifier" {
  description = "Base name for the cluster and all child resources."
  type        = string
}

variable "engine" {
  description = "Database engine family: 'mysql' maps to aurora-mysql, 'postgres' maps to aurora-postgresql."
  type        = string
  default     = "postgres"
  validation {
    condition     = contains(["mysql", "postgres"], var.engine)
    error_message = "Must be 'mysql' or 'postgres'."
  }
}

variable "database_name" {
  description = "Name of the initial database created inside the cluster."
  type        = string
}

variable "master_username" {
  description = "Master DB username. Sensitive — do not log or output this value."
  type        = string
  sensitive   = true
}

variable "vpc_id" {
  description = "VPC in which to place the cluster."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the DB subnet group. Minimum two AZs required."
  type        = list(string)
  validation {
    condition     = length(var.subnet_ids) >= 2
    error_message = "At least two subnets in different AZs are required."
  }
}

variable "allowed_security_group_ids" {
  description = "Security group IDs (e.g. Lambda, ECS tasks) allowed to connect to RDS."
  type        = list(string)
  default     = []
}

variable "allowed_cidr_blocks" {
  description = "CIDR blocks allowed DB access. Prefer SG-based access; use CIDRs only for bastion/VPN scenarios."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags merged onto every resource."
  type        = map(string)
  default     = {}
}
