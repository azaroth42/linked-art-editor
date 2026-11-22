/**
 * Typed HTTP client for the `lae api` process.
 *
 * `schema.d.ts` is generated from the backend's OpenAPI document (`corepack pnpm api:generate`;
 * CI runs `api:check` and fails when it is stale), so every path, parameter, request body, and
 * response below is checked by the compiler. Requests go to the page's own origin (the Vite dev
 * and preview servers proxy the API paths, see `vite.config.ts`) and always send the session
 * cookie. A non-2xx response becomes an {@link ApiError} carrying the backend's structured
 * `{code, path, params}` body (`lae.api.errors.ApiError`); the UI looks the code up in the
 * `errors` namespace and never shows server prose.
 */
import createClient, { type Client } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

/** The structured error body every `ApiError` response of the backend carries. */
export interface ErrorBody {
  code: string;
  path: string;
  params: Record<string, unknown>;
}

/** Code of an {@link ApiError} whose response body was not a structured error body. */
export function httpErrorCode(status: number): string {
  return `HTTP-${String(status)}`;
}

function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const body = value as Record<string, unknown>;
  return (
    typeof body.code === "string" &&
    typeof body.path === "string" &&
    typeof body.params === "object" &&
    body.params !== null
  );
}

/** A non-2xx API response. */
export class ApiError extends Error {
  /** HTTP status code. */
  readonly status: number;
  /** Diagnostic code (`LAE-<AREA>-<NNN>`), or {@link httpErrorCode} for an unstructured body. */
  readonly code: string;
  /** The request path (or JSON Pointer) the error refers to. */
  readonly path: string;
  /** Values the localized message interpolates. */
  readonly params: Record<string, unknown>;
  /** The parsed response body, e.g. FastAPI's `{detail}` for a 422. */
  readonly body: unknown;

  constructor(status: number, body: unknown, requestPath = "") {
    const structured = isErrorBody(body) ? body : undefined;
    const code = structured?.code ?? httpErrorCode(status);
    super(code);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.path = structured?.path ?? requestPath;
    this.params = structured?.params ?? {};
    this.body = body;
  }
}

export type ApiClient = Client<paths>;

export interface ApiClientOptions {
  /** Origin the paths are resolved against; defaults to the page's origin. */
  baseUrl?: string;
  /** Replaces `fetch` (tests); by default the global `fetch` is looked up on every request. */
  fetch?: (request: Request) => Promise<Response>;
}

/** Create a client; requests always include credentials (the `lae_session` cookie). */
export function createApiClient(options: ApiClientOptions = {}): ApiClient {
  return createClient<paths>({
    baseUrl: options.baseUrl ?? globalThis.location.origin,
    credentials: "include",
    headers: { Accept: "application/json" },
    fetch: options.fetch ?? ((request) => globalThis.fetch(request)),
  });
}

/** The application's client. */
export const api: ApiClient = createApiClient();

/** The shape of every openapi-fetch result. */
interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Await an openapi-fetch call and return its data, or throw an {@link ApiError} for a non-2xx
 * response. Network failures reject with the `fetch` error unchanged.
 */
export async function unwrap<T>(pending: Promise<FetchResult<T>>): Promise<T> {
  const { data, error, response } = await pending;
  if (!response.ok) {
    const requestPath = response.url === "" ? "" : new URL(response.url).pathname;
    throw new ApiError(response.status, error, requestPath);
  }
  return data as T;
}
