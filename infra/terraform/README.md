# pfv terraform: DO data droplet

Terraform Cloud workspace `FlamaCorp/pfv` managing the DigitalOcean
infrastructure for the self-hosted MySQL + Redis pair behind `pfv`. State
and runs live in TFC; this directory holds the configuration.

> The apex landing (AWS S3 + CloudFront + ACM + IAM OIDC) lives in a
> separate workspace `FlamaCorp/pfv-apex` with working directory
> `infra/terraform/apex/` and trigger pattern `infra/terraform/apex/**`.
> Keeping the AWS provisioning isolated from this DigitalOcean workspace
> contains the blast radius and lets each workspace use its own auth
> path (DO API token here, AWS OIDC there). See
> [`apex/README.md`](apex/README.md) for the apex setup.

## Resources managed

| Resource | Purpose |
|---|---|
| `digitalocean_vpc` | Dedicated `10.42.0.0/24` VPC in region `ams3` |
| `digitalocean_droplet` | `pfv-data-01`: `s-1vcpu-2gb` Ubuntu 24.04, hosts MySQL 8 + Redis |
| `digitalocean_firewall` | SSH 22 from anywhere; MySQL 3306 / Redis 6379 / ICMP from the VPC only |
| `digitalocean_project_resources` | Attaches the droplet to the existing DO `pfv` project |

## Workspace variables

Set in TFC (Workspace -> Variables); never committed.

| Name | Kind | Sensitive | Description |
|---|---|---|---|
| `do_token` | Terraform | yes | Scoped DO API token: droplets / vpcs / firewalls / projects RW, ssh\_keys R |
| `ssh_key_name` | Terraform | no | Name of an SSH key already registered in DO |

Defaults for `region`, `droplet_size`, `droplet_image`, `project_name`,
and `vpc_ip_range` live in `variables.tf` and rarely need overriding.

## Outputs

Read from TFC -> Workspace -> Outputs after apply.

- `droplet_public_ipv4`: SSH bootstrap target
- `droplet_private_ipv4`: for `DATABASE_URL` / `REDIS_URL` in `.do/app.yaml`
- `vpc_id`: for App Platform's top-level `vpc:` block
- `vpc_ip_range`: VPC CIDR (echoed for Ansible inventory)
- `droplet_id`: numeric ID

## Workflow

- **Speculative plan**: every PR touching `infra/terraform/**` gets a TFC
  plan posted as a status check on the PR.
- **Apply**: triggered automatically on merge to `main`, gated on
  **manual confirm** in the TFC UI. Auto-apply is intentionally off so
  no infra change ever lands without an operator clicking through.
- **Local CLI**: `terraform login` once, then
  `terraform -chdir=infra/terraform plan` reaches the same remote state.

## Module layout

```
.
├── main.tf            cloud{} (TFC), provider, module wiring, project attachment
├── variables.tf
├── outputs.tf
└── modules/
    ├── vpc/           digitalocean_vpc
    ├── droplet/       digitalocean_droplet (monitoring on by default; backups off, see main.tf)
    └── firewall/      digitalocean_firewall (defence-in-depth pair with host ufw)
```

Each child module ships its own `versions.tf` with the
`digitalocean/digitalocean ~> 2.40` provider pin so `terraform init`
resolves the right namespace inside the module.

## Cost

| Line | Monthly |
|---|---|
| `s-1vcpu-2gb` droplet | ~$12.00 |
| DO weekly snapshots | $0 (disabled — nightly mysqldump cron is the durability floor) |
| VPC + firewall + project attachment | $0 |
| **Total** | **~$12.00** |

Replaces ~$30/mo of DO Managed MySQL + Managed Redis. The 2 GB tier (vs.
the 1 GB launch tier) gives the InnoDB buffer pool room to absorb the
write rate from the L4.7 audit log and L4.9 enriched request logs.

## See also

- `../README.md`: overall infra workflow + day-2 operations
- `../MIGRATION.md`: managed -> droplet data-cutover runbook
- `../ansible/`: Ubuntu 24.04 bootstrap that runs after `terraform apply`
