terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.36"
    }
  }
}

provider "aws" {
  region              = "ap-northeast-2"
  allowed_account_ids = ["265233844540"]
}

# Keep the existing CloudFormation ownership boundary. Terraform manages the
# stacks, not their child ECR/IAM/RDS resources individually.
resource "aws_cloudformation_stack" "core" {
  name          = "onedeploy-core"
  template_body = file("${path.module}/../../onedeploy/infra/aws-ecs-express.yaml")
  capabilities  = ["CAPABILITY_IAM"]
  tags = {
    onedeploy-managed = "true"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_security_group" "demo_app_service" {
  name        = "onedeploy-demo-app-service"
  description = "Dedicated OneDeploy demo-app ECS service access to PostgreSQL"
  vpc_id      = "vpc-091d4e4ddec1e37ba"

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    onedeploy-managed = "true"
    onedeploy-app     = "demo-app"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_cloudformation_stack" "demo_app_database" {
  name          = "onedeploy-db-demo-app"
  template_body = file("${path.module}/../../onedeploy/infra/aws-postgres.json")
  capabilities  = ["CAPABILITY_IAM"]
  parameters = {
    ApplicationId          = "demo-app"
    EngineVersion          = "18.3"
    VpcId                  = "vpc-091d4e4ddec1e37ba"
    SubnetIds              = "subnet-02a928d7d81c0f1c4,subnet-06b51abbad1ec0715"
    ServiceSecurityGroupId = aws_security_group.demo_app_service.id
  }
  tags = {
    onedeploy-managed = "true"
    onedeploy-app     = "demo-app"
  }

  lifecycle {
    prevent_destroy = true
    # The AWS provider currently reads this imported stack's parameters as an
    # empty map even though CloudFormation returns them. Keep creation inputs
    # above, but never update the live database stack from that import drift.
    ignore_changes = [parameters]
  }
}

# This demo service has no persistent data. Its ECR image tag is held by the
# core stack repository, while Express owns its generated ALB and security group.
resource "aws_ecs_express_gateway_service" "demo_web" {
  service_name            = "onedeploy-8265dc56c6d74adf-a1"
  cluster                 = "default"
  execution_role_arn      = aws_cloudformation_stack.core.outputs["ExecutionRoleArn"]
  infrastructure_role_arn = aws_cloudformation_stack.core.outputs["InfrastructureRoleArn"]
  cpu                     = "1024"
  memory                  = "2048"
  health_check_path       = "/"
  tags = {
    onedeploy-managed = "true"
    onedeploy-attempt = "8265dc56c6d74adf-a1"
  }

  primary_container {
    image          = "265233844540.dkr.ecr.ap-northeast-2.amazonaws.com/onedeploy-managed:8265dc56c6d74adf-a1"
    container_port = 3000
    aws_logs_configuration {
      log_group         = "/aws/ecs/default/onedeploy-8265dc56c6d74adf-a1-37dc"
      log_stream_prefix = "ecs"
    }
  }

  # OneDeploy remains the service operator. Terraform records/imports this
  # existing deployment without issuing update revisions during handoff.
  # Express chooses a task group and endpoint if the demo is recreated.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}

import {
  to = aws_cloudformation_stack.core
  id = "onedeploy-core"
}

import {
  to = aws_security_group.demo_app_service
  id = "sg-073eaa6496c09b23c"
}

import {
  to = aws_cloudformation_stack.demo_app_database
  id = "onedeploy-db-demo-app"
}

import {
  to = aws_ecs_express_gateway_service.demo_web
  id = "arn:aws:ecs:ap-northeast-2:265233844540:service/default/onedeploy-8265dc56c6d74adf-a1"
}
