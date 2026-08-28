provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = "tbd"
      component   = "mysql-offhost-backup"
      managed_by  = "terraform"
      workspace   = "FlamaCorp/tbd-backups"
      environment = "prod"
    }
  }
}
