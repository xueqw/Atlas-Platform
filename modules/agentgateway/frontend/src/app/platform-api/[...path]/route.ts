import { NextRequest, NextResponse } from "next/server";

const PLATFORM_API_ORIGIN = process.env.PLATFORM_API_ORIGIN || "http://127.0.0.1:8000";
const ALLOWED_RESOURCES = new Set(["skills", "connectors"]);

type RouteContext = { params: Promise<{ path: string[] }> };

export async function GET(request: NextRequest, context: RouteContext) {
  const { path } = await context.params;
  const resource = path.join("/");
  if (!ALLOWED_RESOURCES.has(resource)) {
    return NextResponse.json({ detail: "Unsupported platform resource" }, { status: 404 });
  }

  const upstream = await fetch(`${PLATFORM_API_ORIGIN}/api/${resource}`, {
    headers: {
      accept: "application/json",
      cookie: request.headers.get("cookie") || "",
    },
    cache: "no-store",
  });

  const body = await upstream.arrayBuffer();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "content-type": upstream.headers.get("content-type") || "application/json",
      "cache-control": "no-store",
    },
  });
}
