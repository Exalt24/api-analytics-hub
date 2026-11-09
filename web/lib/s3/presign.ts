/**
 * A presigned upload for a source document attached to a report.
 *
 * The browser PUTs the file straight to the bucket with a URL this server signed, so
 * the bytes never pass through the Next server. The URL is for one key, one content
 * type and one length, and expires in five minutes. The key is built here under the
 * tenant's own prefix; the browser never chooses it.
 *
 * The client reads its endpoint, region and credentials from the environment, so the
 * same code runs against any S3 compatible endpoint (`S3_ENDPOINT`, path style).
 */
import { randomUUID } from "node:crypto";
import { GetObjectCommand, PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

export const UPLOAD_URL_TTL_SECONDS = 300;

export type S3Config = {
  bucket: string;
  region: string;
  endpoint?: string;
  accessKeyId?: string;
  secretAccessKey?: string;
};

export function s3ConfigFromEnv(env: Record<string, string | undefined> = process.env): S3Config | null {
  const bucket = env.S3_BUCKET;
  if (!bucket) return null;
  return {
    bucket,
    region: env.S3_REGION ?? env.AWS_REGION ?? "us-east-1",
    ...(env.S3_ENDPOINT ? { endpoint: env.S3_ENDPOINT } : {}),
    ...(env.S3_ACCESS_KEY_ID ? { accessKeyId: env.S3_ACCESS_KEY_ID } : {}),
    ...(env.S3_SECRET_ACCESS_KEY ? { secretAccessKey: env.S3_SECRET_ACCESS_KEY } : {}),
  };
}

export function makeS3Client(config: S3Config): S3Client {
  return new S3Client({
    region: config.region,
    // The SDK's default adds a CRC32 of the request body to a presigned URL, and at signing time the body is empty, so
    // the URL carries the checksum of nothing and the storage service refuses the real upload as a bad digest.
    requestChecksumCalculation: "WHEN_REQUIRED",
    ...(config.endpoint ? { endpoint: config.endpoint, forcePathStyle: true } : {}),
    ...(config.accessKeyId && config.secretAccessKey
      ? { credentials: { accessKeyId: config.accessKeyId, secretAccessKey: config.secretAccessKey } }
      : {}),
  });
}

/** A file name safe inside a key: letters, digits, dot, dash, underscore; at most 100 characters; never a leading dot. */
export function safeFilename(name: string): string {
  const base = name.split(/[\\/]/).pop() ?? "";
  const cleaned = base.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^\.+/, "").slice(-100);
  return cleaned === "" ? "document" : cleaned;
}

export function objectKeyFor(tenantId: string, month: string, filename: string, id: string = randomUUID()): string {
  return `tenants/${tenantId}/reports/${month}/${id}-${safeFilename(filename)}`;
}

export type PresignedUpload = {
  url: string;
  objectKey: string;
  /** Headers the browser must send with the PUT; the signature covers them. */
  headers: Record<string, string>;
  expiresInSeconds: number;
};

export type UploadParams = {
  tenantId: string;
  month: string;
  filename: string;
  contentType: string;
  size: number;
};

export async function presignUpload(
  client: S3Client,
  config: S3Config,
  params: UploadParams,
  expiresInSeconds: number = UPLOAD_URL_TTL_SECONDS,
): Promise<PresignedUpload> {
  const objectKey = objectKeyFor(params.tenantId, params.month, params.filename);
  const command = new PutObjectCommand({
    Bucket: config.bucket,
    Key: objectKey,
    ContentType: params.contentType,
    ContentLength: params.size,
  });
  const url = await getSignedUrl(client, command, {
    expiresIn: expiresInSeconds,
    signableHeaders: new Set(["content-type", "content-length"]),
  });
  return {
    url,
    objectKey,
    headers: { "Content-Type": params.contentType, "Content-Length": String(params.size) },
    expiresInSeconds,
  };
}

/** A short lived link to read an attached document back, for the tenant that owns the key. */
export async function presignDownload(client: S3Client, config: S3Config, tenantId: string, objectKey: string): Promise<string> {
  if (!objectKey.startsWith(`tenants/${tenantId}/`)) throw new Error("object key belongs to another tenant");
  return getSignedUrl(client, new GetObjectCommand({ Bucket: config.bucket, Key: objectKey }), {
    expiresIn: UPLOAD_URL_TTL_SECONDS,
  });
}
