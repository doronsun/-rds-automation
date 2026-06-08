# ---------------------------------------------------------------------------
# Connection endpoints
# ---------------------------------------------------------------------------
output "cluster_endpoint" {
  description = "Writer endpoint — use for INSERT/UPDATE/DELETE connections."
  value       = aws_rds_cluster.this.endpoint
}

output "cluster_reader_endpoint" {
  description = "Reader endpoint — load-balanced across read replicas (prod only has replicas)."
  value       = aws_rds_cluster.this.reader_endpoint
}

output "cluster_port" {
  description = "Port the cluster listens on (3306 for MySQL, 5432 for PostgreSQL)."
  value       = aws_rds_cluster.this.port
}

output "database_name" {
  description = "Name of the initial database."
  value       = aws_rds_cluster.this.database_name
}

# ---------------------------------------------------------------------------
# Identifiers — used by downstream modules (e.g. Lambda event source, alarms)
# ---------------------------------------------------------------------------
output "cluster_identifier" {
  description = "Full cluster identifier as created in AWS."
  value       = aws_rds_cluster.this.cluster_identifier
}

output "cluster_arn" {
  description = "ARN of the RDS cluster."
  value       = aws_rds_cluster.this.arn
}

# ---------------------------------------------------------------------------
# Secrets Manager — Lambda reads credentials from here; no password in env vars
# ---------------------------------------------------------------------------
output "secret_arn" {
  description = "ARN of the Secrets Manager secret. Grant Lambda GetSecretValue on this ARN."
  value       = aws_secretsmanager_secret.db_credentials.arn
}

output "secret_name" {
  description = "Human-readable name of the Secrets Manager secret."
  value       = aws_secretsmanager_secret.db_credentials.name
}

# ---------------------------------------------------------------------------
# Networking — consumed by the SAM template to wire Lambda into the same VPC
# ---------------------------------------------------------------------------
output "security_group_id" {
  description = "ID of the RDS security group. Add this to Lambda's allowed_security_group_ids."
  value       = aws_security_group.rds.id
}

output "db_subnet_group_name" {
  description = "Name of the DB subnet group."
  value       = aws_db_subnet_group.this.name
}
