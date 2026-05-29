"""
Cloudflare R2 client — stockage documents employes.
Utilise boto3 avec l'API S3-compatible de R2.
"""
import os
import boto3
from botocore.config import Config


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("R2_ENDPOINT_URL", ""),
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _bucket():
    return os.environ.get("R2_BUCKET_NAME", "pauco-documents")


def upload_file(file_obj, folder, filename):
    """Upload un fichier vers R2. Retourne la cle S3."""
    key = f"{folder}/{filename}"
    client = _get_client()
    client.upload_fileobj(
        file_obj, _bucket(), key,
        ExtraArgs={"ContentType": file_obj.content_type or "application/octet-stream"},
    )
    return key


def upload_bytes(data, key, content_type="application/octet-stream"):
    """Upload raw bytes vers R2."""
    from io import BytesIO
    client = _get_client()
    client.upload_fileobj(
        BytesIO(data), _bucket(), key,
        ExtraArgs={"ContentType": content_type},
    )
    return key


def delete_file(key):
    """Supprime un fichier de R2."""
    client = _get_client()
    client.delete_object(Bucket=_bucket(), Key=key)


def get_file_url(key, expires=3600):
    """Genere une URL pre-signee pour telecharger un fichier."""
    client = _get_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": key},
        ExpiresIn=expires,
    )


def is_configured():
    """Retourne True si les variables R2 sont configurees."""
    return bool(os.environ.get("R2_ACCESS_KEY_ID"))
