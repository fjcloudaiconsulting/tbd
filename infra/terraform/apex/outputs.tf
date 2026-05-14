output "s3_bucket_name" {
  description = "Name of the apex landing S3 bucket. Consumed by PR-B's GitHub Actions workflow for `aws s3 sync`."
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
  description = "CloudFront distribution ID. Consumed by PR-B's workflow for `aws cloudfront create-invalidation`. Also the target for PR-D's apex A ALIAS record."
  value       = aws_cloudfront_distribution.apex.id
}

output "cloudfront_distribution_arn" {
  description = "Full ARN of the CloudFront distribution. Used by the bucket policy's SourceArn condition and the GitHub Actions inline policy's invalidation scope."
  value       = aws_cloudfront_distribution.apex.arn
}

output "cloudfront_distribution_domain" {
  description = "CloudFront-assigned dXXX.cloudfront.net hostname. This is the pre-cutover verification URL: PR-C's static export is browsable here once PR-B has synced it. PR-D's Route 53 ALIAS will point to this distribution by its ID, not this hostname."
  value       = aws_cloudfront_distribution.apex.domain_name
}

output "cloudfront_distribution_hosted_zone_id" {
  description = "CloudFront's fixed hosted zone ID (Z2FDTNDATAQYW2). PR-D will need this when constructing the apex A ALIAS record."
  value       = aws_cloudfront_distribution.apex.hosted_zone_id
}

output "acm_certificate_arn" {
  description = "ARN of the validated us-east-1 ACM certificate for apex + www."
  value       = aws_acm_certificate_validation.apex.certificate_arn
}

output "route53_zone_id" {
  description = "Hosted zone ID for the apex domain. Consumed by PR-D for the apex A ALIAS swap."
  value       = data.aws_route53_zone.apex.zone_id
}

output "github_actions_role_arn" {
  description = "ARN of the IAM role GitHub Actions assumes via OIDC. PR-B sets this as `role-to-assume` in aws-actions/configure-aws-credentials."
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
