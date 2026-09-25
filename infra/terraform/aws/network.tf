module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.7"

  name = local.name
  cidr = "10.42.0.0/16"
  azs  = local.azs

  private_subnets     = ["10.42.0.0/19", "10.42.32.0/19"]
  public_subnets      = ["10.42.64.0/22", "10.42.68.0/22"]
  database_subnets    = ["10.42.80.0/24", "10.42.81.0/24"]
  elasticache_subnets = ["10.42.84.0/24", "10.42.85.0/24"]

  create_database_subnet_group    = true
  create_elasticache_subnet_group = true

  enable_nat_gateway   = true
  single_nat_gateway   = true # demo cost trade-off; one NAT per AZ in production
  enable_dns_hostnames = true
  enable_dns_support   = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# Free gateway endpoint keeps S3 traffic (datasets, lake) off the NAT and inside AWS.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids
}
