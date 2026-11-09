import { CreateBucketCommand } from "@aws-sdk/client-s3";
import { makeS3Client } from "../lib/s3/presign";
import { buildDatabase, unavailableReason } from "../tests/db-build";

const E2E_DB = "analytics_web_e2e";
const S3_ENDPOINT = process.env.S3_TEST_ENDPOINT ?? "http://127.0.0.1:8333";

export default async function globalSetup(): Promise<void> {
  const why = await unavailableReason();
  if (why) throw new Error(`The browser test needs Postgres: ${why}. Run docker compose up -d --wait.`);
  await buildDatabase(E2E_DB);

  let s3Up = false;
  try {
    const res = await fetch(S3_ENDPOINT, { signal: AbortSignal.timeout(2000) });
    s3Up = res.status === 403 || res.status === 200;
  } catch {
    s3Up = false;
  }
  if (!s3Up && process.env.WEB_REQUIRE_S3 === "1") throw new Error(`WEB_REQUIRE_S3=1 but the S3 stand-in is not reachable at ${S3_ENDPOINT}`);
  process.env.E2E_S3_UP = s3Up ? "1" : "";
  if (s3Up) {
    const client = makeS3Client({
      bucket: "report-documents",
      region: "us-east-1",
      endpoint: S3_ENDPOINT,
      accessKeyId: "webtestaccesskey",
      secretAccessKey: "webtest-secret-key-not-real",
    });
    try {
      await client.send(new CreateBucketCommand({ Bucket: "report-documents" }));
    } catch (err) {
      if (!(err instanceof Error) || !/BucketAlreadyOwnedByYou|BucketAlreadyExists/.test(err.name + err.message)) throw err;
    }
  }
}
