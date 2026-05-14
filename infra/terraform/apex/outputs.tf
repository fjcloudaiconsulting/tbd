output "s3_bucket_name" {
  description = "Name of the apex landing S3 bucket. Consumed by `apex-deploy.yml` (GitHub repo variable `AWS_APEX_BUCKET`) for `aws s3 sync`."
  value       = aws_s3_bucket.apex.id
}

output "s3_bucket_arn" {
  description = "ARN of the apex landing S3 bucket. Useful for cross-account integrations and future log-shipping policies."
  value       = aws_s3_bucket.apex.arn
}

output "s3_bucket_regional_domain_name" {
  description = "Regional domain name of the apex bucket (S3 -> CloudFront origin DNS target). Echoed for debugging."
  value       = aws_s3_bucket.apex.bucket_regional_domain_name
}

output "cloudfront_distribution_id" {
  description = "CloudFront distribution ID. Consumed by `apex-deploy.yml` (GitHub repo variable `AWS_APEX_DISTRIBUTION_ID`) for `aws cloudfront create-invalidation`. Also the alias target for the apex / www A and AAAA ALIAS records in this module."
  value       = aws_cloudfront_distribution.apex.id
}

output "cloudfront_distribution_arn" {
  description = "Full ARN of the CloudFront distribution. Used by the bucket policy's SourceArn condition and the GitHub Actions inline policy's invalidation scope."
  value       = aws_cloudfront_distribution.apex.arn
}

output "cloudfront_distribution_domain" {
  description = "CloudFront-assigned dXXX.cloudfront.net hostname. Diagnostic / fallback path: the apex / www ALIAS records send public traffic via DNS to this distribution, but the dXXX hostname stays reachable for probing the distribution directly when the apex hostname is itself unreachable."
  value       = aws_cloudfront_distribution.apex.domain_name
}

output "cloudfront_distribution_hosted_zone_id" {
  description = "CloudFront's fixed hosted zone ID (`Z2FDTNDATAQYW2`). Referenced internally by this module's apex / www ALIAS records via `alias.zone_id`. Exposed as an output for visibility and for any future consumer that needs to construct a Route 53 ALIAS targeting this distribution."
  value       = aws_cloudfront_distribution.apex.hosted_zone_id
}

output "acm_certificate_arn" {
  description = "ARN of the validated us-east-1 ACM certificate for apex + www."
  value       = aws_acm_certificate_validation.apex.certificate_arn
}

output "route53_zone_id" {
  description = "Hosted zone ID for the apex domain. Referenced internally by the ACM validation CNAMEs and the apex / www ALIAS records in this module."
  value       = data.aws_route53_zone.apex.zone_id
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role GitHub Actions assumes via OIDC. `apex-deploy.yml` consumes this (GitHub repo variable `AWS_APEX_DEPLOY_ROLE_ARN`) as `role-to-assume` in aws-actions/configure-aws-credentials."
  value       = aws_iam_role.github_actions_apex_deploy.arn
}

output "tfc_role_arn" {
  description = "ARN of the IAM role TFC assumes via workload identity. Set this in the FlamaCorp/pfv-apex workspace's TFC_AWS_RUN_ROLE_ARN variable so TFC stops needing static credentials after the bootstrap apply."
  value       = aws_iam_role.tfc_apex_provisioner.arn
}

output "github_oidc_provider_arn" {
  description = "ARN of the GitHub Actions OIDC provider. Surface this so a future module (or a second repo) can attach roles to the same provider without re-creating it."
  value       = aws_iam_openid_connect_provider.github.arn
}

output "tfc_oidc_provider_arn" {
  description = "ARN of the Terraform Cloud OIDC provider. Same rationale as github_oidc_provider_arn."
  value       = aws_iam_openid_connect_provider.tfc.arn
}

output "apex_domain" {
  description = "Echo of the apex domain configured by var.domain. Useful for sanity-checking the workspace at a glance."
  value       = var.domain
}
