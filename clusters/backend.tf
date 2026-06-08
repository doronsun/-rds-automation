terraform {
  required_version = ">= 1.6"

  # Partial S3 backend configuration.
  # All values (bucket, key, region) are injected at runtime by CircleCI
  # via -backend-config flags — nothing sensitive is stored here.
  #
  # To initialise locally:
  #   terraform init \
  #     -backend-config="bucket=your-tf-state-bucket" \
  #     -backend-config="key=clusters/terraform.tfstate" \
  #     -backend-config="region=us-east-1"
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.5"
    }
  }
}

provider "aws" {
  # Region is set via AWS_DEFAULT_REGION environment variable in CI.
  # For local runs: export AWS_DEFAULT_REGION=us-east-1
}
