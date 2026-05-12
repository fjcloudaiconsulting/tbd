# Default AWS provider: hosts the S3 bucket, IAM, and Route 53 lookups.
# Region is configurable via var.aws_region; default eu-west-1 matches the
# rest of the EU-centric pfv stack.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "pfv"
      component   = "apex-landing"
      managed_by  = "terraform"
      workspace   = "FlamaCorp/pfv-apex"
      environment = "prod"
    }
  }
}

# us-east-1 alias provider. CloudFront requires that the ACM certificate
# attached to a distribution live in us-east-1 regardless of where the
# origin bucket sits. Only used for aws_acm_certificate + its validation.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      project     = "pfv"
      component   = "apex-landing"
      managed_by  = "terraform"
      workspace   = "FlamaCorp/pfv-apex"
      environment = "prod"
    }
  }
}
