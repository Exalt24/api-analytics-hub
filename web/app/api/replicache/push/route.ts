import { respond } from "@/lib/replicache/route";
import { handlePush } from "@/lib/replicache/server";

export async function POST(request: Request): Promise<Response> {
  return respond(request, handlePush);
}
