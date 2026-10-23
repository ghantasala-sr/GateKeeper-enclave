// Modules C1/C2: steady traffic through the real Gateway while something disruptive
// happens (helm upgrade, helm rollback, kubectl drain). Passes only with ZERO failed
// requests. No model calls: chat would measure Vertex AI, not the deployment.
//
//   tools/k6 run -e DURATION=5m proof/k6/availability.js
import http from "k6/http";
import { check } from "k6";

const GATEWAY = __ENV.GATEWAY || "http://10.10.0.100:8088";
const APP = { headers: { Host: "gatekeeper.localhost" } };
const AUTH = { headers: { Host: "auth.gatekeeper.localhost" } };

export const options = {
  scenarios: {
    steady: {
      executor: "constant-arrival-rate",
      rate: Number(__ENV.RATE || 20), timeUnit: "1s",
      duration: __ENV.DURATION || "5m",
      preAllocatedVUs: 20, maxVUs: 100,
    },
  },
  thresholds: {
    http_req_failed: ["rate==0"],            // not one failed request
    checks: ["rate==1"],
    http_req_duration: ["p(95)<1000"],
  },
  summaryTrendStats: ["avg", "p(95)", "p(99)", "max"],
};

const targets = [
  ["frontend /", () => http.get(`${GATEWAY}/`, APP)],
  ["api /ready (database)", () => http.get(`${GATEWAY}/api/ready`, APP)],
  ["api jwks", () => http.get(`${GATEWAY}/api/.well-known/jwks.json`, APP)],
  ["keycloak discovery", () => http.get(`${GATEWAY}/realms/acme/.well-known/openid-configuration`, AUTH)],
];

export default function () {
  const [name, call] = targets[__ITER % targets.length];
  const res = call();
  check(res, { [`${name} 200`]: (r) => r.status === 200 });
}
