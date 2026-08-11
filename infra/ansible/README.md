# <data-droplet> Ansible

Configuration management for the pfv data droplet (MySQL + Redis).

Provisioning lives in Terraform (`infra/terraform/`); this playbook handles
post-boot config: packages, MySQL/Redis tuning, backups, fail2ban, swap.

## Run

```bash
ansible-playbook -i infra/ansible/inventory.yml \
  infra/ansible/playbooks/site.yml --limit <data-droplet>
```

## Firewall: single layer, DO cloud firewall only

The DigitalOcean cloud firewall `<data-firewall>` is the single source of truth
for inbound rules on managed droplets. UFW is intentionally **disabled** by
the `common` role.

### Why

Layering UFW on top of the DO cloud firewall risks silent drops during VPC
NAT translation: a TCP SYN can reach the droplet's VPC interface from a
rewritten source address that UFW's CIDR rule no longer matches. Symptoms
look like generic connectivity timeouts (App Platform to Redis on
`<vpc-cidr>` was the case that triggered this consolidation on 2026-05-13).

### Rules enforced by `<data-firewall>`

- TCP 3306 (MySQL): from `<vpc-cidr>`
- TCP 6379 (Redis): from `<vpc-cidr>`
- TCP 22 (SSH): from `0.0.0.0/0`
- ICMP: from the VPC subnet

If you need to add a rule, edit the DO cloud firewall in Terraform (or via
`doctl compute firewall update`). Do not re-add UFW tasks to the `common`
role.
