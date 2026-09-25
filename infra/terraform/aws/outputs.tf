output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "namespace" {
  value = local.namespace
}

output "iot_endpoint" {
  value = data.aws_iot_endpoint.ats.endpoint_address
}

output "kinesis_stream" {
  value = aws_kinesis_stream.telemetry.name
}

output "queues" {
  value = { for k, q in aws_sqs_queue.q : k => q.url }
}

output "redis_url" {
  value = "rediss://${aws_elasticache_replication_group.redis.primary_endpoint_address}:6379/0"
}

output "db_host" {
  value = aws_db_instance.main.address
}

output "db_secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}

output "database_url" {
  value     = "postgresql://${aws_db_instance.main.username}:${random_password.db.result}@${aws_db_instance.main.address}:5432/rpm?sslmode=require"
  sensitive = true
}

output "db_username" {
  value = aws_db_instance.main.username
}

output "db_password" {
  value     = random_password.db.result
  sensitive = true
}

output "ecr_registry" {
  value = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "data_bucket" {
  value = aws_s3_bucket.data.bucket
}

output "lake_bucket" {
  value = aws_s3_bucket.lake.bucket
}

output "demo_device_id" {
  value = aws_iot_thing.demo_watch.name
}

output "demo_watch_cert_pem" {
  value     = aws_iot_certificate.demo_watch.certificate_pem
  sensitive = true
}

output "demo_watch_private_key" {
  value     = aws_iot_certificate.demo_watch.private_key
  sensitive = true
}

output "sim_cert_pem" {
  value     = aws_iot_certificate.sim_fleet.certificate_pem
  sensitive = true
}

output "sim_private_key" {
  value     = aws_iot_certificate.sim_fleet.private_key
  sensitive = true
}
