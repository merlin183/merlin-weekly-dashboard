// Optional Cloudflare proxy for the installer-owned Python application.
import { Container } from "@cloudflare/containers";

// 컨테이너로 넘기지 않는 워커 바인딩 이름
const NON_ENV_KEYS = new Set(["APP"]);

interface Env {
  APP: DurableObjectNamespace<AppContainer>;
  SLEEP_AFTER?: string;
  [key: string]: unknown;
}

export class AppContainer extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "15m";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    if (typeof env.SLEEP_AFTER === "string" && env.SLEEP_AFTER) this.sleepAfter = env.SLEEP_AFTER;
    // Forward installer-owned string settings to the application environment.
    const vars: Record<string, string> = {};
    for (const [k, v] of Object.entries(env)) {
      if (typeof v === "string" && !NON_ENV_KEYS.has(k) && k !== "SLEEP_AFTER") vars[k] = v;
    }
    this.envVars = vars;
  }

  override onStart(): void {
    console.log("[container] 시작됨");
  }

  override onStop(): void {
    console.log("[container] 중지됨");
  }

  override onError(error: unknown): never {
    console.error("[container] 오류", error);
    throw error;
  }
}

function appStub(env: Env) {
  return env.APP.get(env.APP.idFromName("main"));
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // 실제 접속자 IP를 X-Forwarded-For 로 고정 — 앱의 로그인 시도 제한이 이 값을 신뢰한다(사용자가 위조 불가하게 덮어씀)
    const headers = new Headers(request.headers);
    const ip = request.headers.get("CF-Connecting-IP");
    if (ip) headers.set("X-Forwarded-For", ip);
    headers.set("X-Forwarded-Proto", "https");
    return appStub(env).fetch(new Request(request, { headers }));
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    // 컨테이너 깨우기 + 활동시간 갱신
    ctx.waitUntil(
      appStub(env)
        .fetch("http://container/api/health")
        .then((r) => console.log(`[cron] health ${r.status}`))
        .catch((e) => console.error("[cron] health 실패", e)),
    );
  },
} satisfies ExportedHandler<Env>;
