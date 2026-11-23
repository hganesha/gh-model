terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

module "network" {
  source = "./modules/network"
}

resource "aws_db_instance" "status" {
  identifier        = "status-db"
  engine            = "postgres"
  instance_class    = "db.t4g.micro"
  allocated_storage = 20
  db_subnet_group_name = module.network.vpc_id
}

resource "aws_ecr_repository" "status_api" {
  name = "status-api"
}
