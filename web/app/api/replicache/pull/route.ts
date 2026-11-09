import { respond } from "@/lib/replicache/route";
import { handlePull } from "@/lib/replicache/server";

export async function POST(request: Request): Promise<Response> {
  return respond(request, handlePull);
}
