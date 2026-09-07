from workers import Response, WorkerEntrypoint


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url
        if "/health" in url:
            return Response.json({"ok": True, "service": "infotrader"})

        return Response.json(
            {
                "service": "infotrader",
                "status": "online",
                "message": "InfoTrader Cloudflare Worker is running.",
            }
        )
