#!/usr/bin/env python3
"""Upload verified nightly backup artifacts to S3, in order, fail-fast.

Usage:
  mysql-backup-upload.py --bucket B --kms-key-id ARN --region R --prefix P FILE [FILE ...]

Files are uploaded in the order given. The caller is responsible for putting
the manifest LAST -- S3 has no rename, so the manifest's presence is the only
thing that distinguishes "this night completed end to end" from "the dump
uploaded and the grants upload then died".

⚠ WHY boto3 AND NOT THE AWS CLI. The droplet is Ubuntu 24.04 with no `awscli`
apt candidate and no `unzip`, so the CLI would mean downloading a ~60 MB zip
from the internet and pinning a digest nobody in this repo can independently
re-derive. `python3-boto3` is already installed from Ubuntu's own signed
repository (measured 2026-08-27: 1.34.46), gets security updates through apt,
and exposes the two features this needs. Fewer moving parts and a better
supply chain.

⚠ WHY THIS IS A FILE AND NOT INLINE IN THE .j2. Logic embedded in a Jinja
template can only ever be grep-fenced, and in this repo a grep is routinely
satisfied by a comment. As a real file the test suite can import and drive it.

Exit 0 all uploaded; 1 an upload failed; 2 bad arguments/environment.
"""
import argparse
import os
import sys

# ⚠ Imported lazily rather than at module scope. boto3 exists on the droplet
# but not necessarily in the test container, and a module-scope import would
# make this file un-importable there -- so the fences could not drive the
# argument handling and file checks below, and would degrade into grepping the
# source. Failing inside main() keeps the logic testable without boto3.
boto3 = None
BotoCoreError = ClientError = Exception


def _load_boto3():
    global boto3, BotoCoreError, ClientError
    if boto3 is not None:
        return True
    try:
        import boto3 as _boto3
        from botocore.exceptions import BotoCoreError as _BCE, ClientError as _CE
    except ImportError:
        return False
    boto3, BotoCoreError, ClientError = _boto3, _BCE, _CE
    return True


def parse_args(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--kms-key-id", required=True)
    ap.add_argument("--region", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("files", nargs="+")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])

    for path in args.files:
        if not os.path.isfile(path):
            sys.stderr.write(f"!! not a file: {path}\n")
            return 2

    if not _load_boto3():
        sys.stderr.write("!! python3-boto3 is not installed on this host\n")
        return 2

    client = boto3.client("s3", region_name=args.region)

    for path in args.files:
        key = f"{args.prefix}/{os.path.basename(path)}"
        try:
            with open(path, "rb") as fh:
                client.put_object(
                    Bucket=args.bucket,
                    Key=key,
                    Body=fh,
                    # ⚠ THIS IS THE TRANSPORT VERIFICATION, not a nicety. S3
                    # recomputes the SHA-256 server-side and rejects a mismatch
                    # with BadDigest, so a successful PUT is proof that the
                    # bytes S3 stored are the bytes we sent -- checked against
                    # the STORED object, with zero read permission. It is what
                    # resolves the "verify the uploaded object using a put-only
                    # credential" contradiction in the ticket.
                    ChecksumAlgorithm="SHA256",
                    # ⚠ Both SSE parameters must be sent EXPLICITLY. The
                    # uploader's IAM policy conditions on them with
                    # StringEquals, and StringEquals against an ABSENT header
                    # fails -- relying on the bucket's default encryption to
                    # satisfy the condition does not work, and the resulting
                    # 403 reads like a credential problem.
                    ServerSideEncryption="aws:kms",
                    SSEKMSKeyId=args.kms_key_id,
                )
        except (BotoCoreError, ClientError) as exc:
            sys.stderr.write(f"!! upload failed for {key}: {exc}\n")
            return 1
        print(f"uploaded s3://{args.bucket}/{key}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
