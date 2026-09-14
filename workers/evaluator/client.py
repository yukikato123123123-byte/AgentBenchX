"""Generic client for the existing authenticated worker protocol."""

import httpx


class WorkerClient:
    def __init__(self, api_url: str, token: str | None = None):
        self.http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=httpx.Timeout(10, connect=5),
            follow_redirects=False,
        )

    def register(self, **metadata) -> dict:
        response = self.http.post("/api/v1/workers/register", json=metadata)
        response.raise_for_status()
        credentials = response.json()
        self.http.headers["Authorization"] = f"Bearer {credentials['token']}"
        return credentials

    def heartbeat(self, worker_id: str, status: str = "ONLINE") -> dict:
        return self._post(f"/workers/{worker_id}/heartbeat", {"status": status})

    def claim(self, worker_id: str) -> dict | None:
        return self._post(f"/workers/{worker_id}/jobs/claim", {})

    def job_heartbeat(self, worker_id: str, job_id: str, lease_token: str, event: str) -> dict:
        return self._post(
            f"/workers/{worker_id}/jobs/{job_id}/heartbeat", {"lease_token": lease_token, "event": event}
        )

    def result(self, worker_id: str, job_id: str, result: dict) -> dict:
        return self._post(f"/workers/{worker_id}/jobs/{job_id}/result", result)

    def _post(self, path: str, payload: dict):
        response = self.http.post("/api/v1" + path, json=payload)
        response.raise_for_status()
        return response.json()

    def close(self):
        self.http.close()

    def download_agent(self, worker_id: str, job: dict) -> bytes:
        # Construct the server path ourselves; never send credentials to a payload-supplied URL.
        path = f"/api/v1/workers/{worker_id}/jobs/{job['id']}/agent"
        with self.http.stream("GET", path, headers={"X-Lease-Token": job["lease_token"]}) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 2_000_000:
                    raise ValueError("Agent download exceeds 2 MB")
            return bytes(data)
