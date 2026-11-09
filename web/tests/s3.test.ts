/**
 * The presigned upload helper, two ways:
 *  - without a server: what the signed URL says (key under the tenant, expiry, signed headers, file name cleaning);
 *  - against a stand-in S3 (SeaweedFS from compose.yaml, profile s3), which checks AWS signature v4 for real:
 *    a correct PUT is stored and read back byte for byte, and a PUT that changes the type, the length, the key
 *    or waits past the expiry is refused.
 * With the stand-in not running these skip, unless WEB_REQUIRE_S3=1 (CI), where a missing one fails.
 */
import { CreateBucketCommand, GetObjectCommand, HeadObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { afterAll, beforeAll, describe, expect, inject, it } from "vitest";
import { createUpload } from "@/lib/s3/upload";
import { createDb, pgDialect, type Db } from "@/lib/server/db";
import { makeS3Client, objectKeyFor, presignDownload, presignUpload, s3ConfigFromEnv, safeFilename, type S3Config } from "@/lib/s3/presign";
import { KEY_A, TENANT_A, TENANT_B } from "./fixtures";

const ENDPOINT = process.env.S3_TEST_ENDPOINT ?? "http://127.0.0.1:8333";
const config: S3Config = {
  bucket: "report-documents",
  region: "us-east-1",
  endpoint: ENDPOINT,
  accessKeyId: "webtestaccesskey",
  secretAccessKey: "webtest-secret-key-not-real",
};
const client = makeS3Client(config);
const UUID_PREFIX = "11111111-1111-4111-8111-111111111111";

describe("keys and file names", () => {
  it("builds the key under the tenant and month, never from the browser's path", () => {
    expect(objectKeyFor(TENANT_A, "2026-10", "source.pdf", UUID_PREFIX)).toBe(`tenants/${TENANT_A}/reports/2026-10/${UUID_PREFIX}-source.pdf`);
    expect(objectKeyFor(TENANT_A, "2026-10", "../../etc/passwd", UUID_PREFIX)).toBe(`tenants/${TENANT_A}/reports/2026-10/${UUID_PREFIX}-passwd`);
    expect(objectKeyFor(TENANT_A, "2026-10", "C:\\Users\\x\\my file (1).pdf", UUID_PREFIX)).toBe(`tenants/${TENANT_A}/reports/2026-10/${UUID_PREFIX}-my_file_1_.pdf`);
  });

  it("cleans a file name to letters, digits, dot, dash and underscore, at most 100 long, never hidden or empty", () => {
    expect(safeFilename(".htaccess")).toBe("htaccess");
    expect(safeFilename("   ")).toBe("_");
    expect(safeFilename("")).toBe("document");
    expect(safeFilename("a".repeat(300) + ".pdf").length).toBe(100);
    expect(safeFilename("na\u00efve \u65e5\u672c.pdf")).toBe("na_ve_.pdf");
  });

  it("reads its config from the environment, and is off without a bucket", () => {
    expect(s3ConfigFromEnv({})).toBeNull();
    expect(s3ConfigFromEnv({ S3_BUCKET: "b", S3_ENDPOINT: "http://x:1", S3_ACCESS_KEY_ID: "k", S3_SECRET_ACCESS_KEY: "s" })).toEqual({
      bucket: "b",
      region: "us-east-1",
      endpoint: "http://x:1",
      accessKeyId: "k",
      secretAccessKey: "s",
    });
  });
});

describe("what the signed URL says", () => {
  it("is for one key, signs the type and length, and expires in 300 seconds", async () => {
    const up = await presignUpload(client, config, { tenantId: TENANT_A, month: "2026-10", filename: "source.pdf", contentType: "application/pdf", size: 1234 });
    const url = new URL(up.url);
    expect(url.pathname).toBe(`/${config.bucket}/${up.objectKey}`);
    expect(up.objectKey.startsWith(`tenants/${TENANT_A}/reports/2026-10/`)).toBe(true);
    expect(url.searchParams.get("X-Amz-Expires")).toBe("300");
    const signed = (url.searchParams.get("X-Amz-SignedHeaders") ?? "").split(";");
    expect(signed).toEqual(expect.arrayContaining(["host", "content-type", "content-length"]));
    expect(up.headers).toEqual({ "Content-Type": "application/pdf", "Content-Length": "1234" });
  });

  it("gives two uploads of the same file two different keys", async () => {
    const p = { tenantId: TENANT_A, month: "2026-10", filename: "same.pdf", contentType: "application/pdf", size: 5 };
    const [a, b] = await Promise.all([presignUpload(client, config, p), presignUpload(client, config, p)]);
    expect(a.objectKey).not.toBe(b.objectKey);
  });

  it("will not sign a read link for another tenant's key", async () => {
    await expect(presignDownload(client, config, TENANT_A, `tenants/${TENANT_B}/reports/2026-10/${UUID_PREFIX}-x.pdf`)).rejects.toThrow(/another tenant/);
  });
});

const adminDsn = inject("adminDsn");
describe.skipIf(!adminDsn)("the upload request, with the key resolved against the database", () => {
  let db: Db;
  beforeAll(() => {
    db = createDb(pgDialect(adminDsn, 2));
  });
  afterAll(async () => {
    await db.destroy();
  });
  const NOW = () => new Date("2026-11-09T00:00:00Z");
  const body = { apiKey: KEY_A, month: "2026-10", filename: "source.pdf", contentType: "application/pdf", size: 2048 };

  it("signs under the tenant of the key, and the browser cannot pick another", async () => {
    const r = await createUpload({ db, s3: { client, config }, now: NOW }, { ...body, tenantId: TENANT_B });
    if (!r.ok) throw new Error(r.message);
    expect(r.objectKey.startsWith(`tenants/${TENANT_A}/`)).toBe(true);
    expect(r.objectKey).not.toContain(TENANT_B);
  });

  it("refuses an unknown key, a type outside the list, 10 MB and a byte, zero bytes, a future month and a missing bucket", async () => {
    const deps = { db, s3: { client, config }, now: NOW };
    expect((await createUpload(deps, { ...body, apiKey: "aah_not_a_real_key_000000" })).ok).toBe(false);
    const codes = async (b: unknown) => {
      const r = await createUpload(deps, b);
      return r.ok ? "ok" : r.code;
    };
    expect(await codes({ ...body, apiKey: "aah_not_a_real_key_000000" })).toBe("unauthorized");
    expect(await codes({ ...body, contentType: "application/x-msdownload" })).toBe("invalid_input");
    expect(await codes({ ...body, size: 10 * 1024 * 1024 + 1 })).toBe("invalid_input");
    expect(await codes({ ...body, size: 10 * 1024 * 1024 })).toBe("ok");
    expect(await codes({ ...body, size: 0 })).toBe("invalid_input");
    expect(await codes({ ...body, size: 1.5 })).toBe("invalid_input");
    expect(await codes({ ...body, month: "2026-12" })).toBe("invalid_input");
    expect(await createUpload({ db, s3: null, now: NOW }, body)).toMatchObject({ ok: false, code: "not_configured" });
  });
});

async function standInUp(): Promise<boolean> {
  try {
    const res = await fetch(ENDPOINT, { signal: AbortSignal.timeout(2000) });
    return res.status === 403 || res.status === 200;
  } catch {
    return false;
  }
}

const required = process.env.WEB_REQUIRE_S3 === "1";
const standIn = await standInUp();
if (required && !standIn) throw new Error("WEB_REQUIRE_S3=1 but the S3 stand-in is not reachable at " + ENDPOINT);

describe.skipIf(!standIn)("against a stand-in S3 that verifies AWS signature v4 (SeaweedFS)", () => {
  const params = { tenantId: TENANT_A, month: "2026-10", filename: "source.csv", contentType: "text/csv", size: 0 };
  const payload = Buffer.from("a,b\n1,2\n3,4\n", "utf8");

  beforeAll(async () => {
    try {
      await client.send(new CreateBucketCommand({ Bucket: config.bucket }));
    } catch (err) {
      if (!(err instanceof Error) || !/BucketAlreadyOwnedByYou|BucketAlreadyExists/.test(err.name + err.message)) throw err;
    }
  });

  it("stores a correct PUT and returns the same bytes, through the client and through a presigned read link", async () => {
    const up = await presignUpload(client, config, { ...params, size: payload.length });
    const put = await fetch(up.url, { method: "PUT", headers: up.headers, body: payload });
    expect(put.status).toBe(200);
    const head = await client.send(new HeadObjectCommand({ Bucket: config.bucket, Key: up.objectKey }));
    expect(head.ContentLength).toBe(payload.length);
    expect(head.ContentType).toBe("text/csv");
    const got = await client.send(new GetObjectCommand({ Bucket: config.bucket, Key: up.objectKey }));
    expect(Buffer.from(await got.Body!.transformToByteArray()).equals(payload)).toBe(true);
    const link = await presignDownload(client, config, TENANT_A, up.objectKey);
    const read = await fetch(link);
    expect(read.status).toBe(200);
    expect(Buffer.from(await read.arrayBuffer()).equals(payload)).toBe(true);
  });

  it("refuses a PUT that changes the content type", async () => {
    const up = await presignUpload(client, config, { ...params, size: payload.length });
    const put = await fetch(up.url, { method: "PUT", headers: { ...up.headers, "Content-Type": "text/html" }, body: payload });
    expect(put.status).toBe(403);
    await expect(client.send(new HeadObjectCommand({ Bucket: config.bucket, Key: up.objectKey }))).rejects.toThrow();
  });

  it("refuses a PUT whose body is not the length that was signed", async () => {
    const up = await presignUpload(client, config, { ...params, size: payload.length });
    const longer = Buffer.concat([payload, Buffer.from("extra")]);
    const put = await fetch(up.url, { method: "PUT", body: longer, headers: { "Content-Type": "text/csv" } });
    expect(put.status).toBe(403);
    await expect(client.send(new HeadObjectCommand({ Bucket: config.bucket, Key: up.objectKey }))).rejects.toThrow();
  });

  it("refuses a URL whose key has been changed, so one signature cannot write another object", async () => {
    const up = await presignUpload(client, config, { ...params, size: payload.length });
    const tampered = up.url.replace(`tenants/${TENANT_A}/`, `tenants/${TENANT_B}/`);
    expect(tampered).not.toBe(up.url);
    const put = await fetch(tampered, { method: "PUT", headers: up.headers, body: payload });
    expect(put.status).toBe(403);
  });

  it("refuses a URL after it has expired", async () => {
    const up = await presignUpload(client, config, { ...params, size: payload.length }, 1);
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const put = await fetch(up.url, { method: "PUT", headers: up.headers, body: payload });
    expect(put.status).toBe(403);
  });

  it("refuses a client with the wrong secret", async () => {
    const wrong = new S3Client({ region: "us-east-1", endpoint: ENDPOINT, forcePathStyle: true, credentials: { accessKeyId: "webtestaccesskey", secretAccessKey: "not-the-secret" } });
    const up = await presignUpload(wrong, config, { ...params, size: payload.length });
    const put = await fetch(up.url, { method: "PUT", headers: up.headers, body: payload });
    expect(put.status).toBe(403);
  });
});
