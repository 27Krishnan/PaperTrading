import asyncio
import httpx

async def test():
    async with httpx.AsyncClient() as client:
        with open("test.jpg", "wb") as f:
            f.write(b"dummy")
            
        with open("test.jpg", "rb") as f:
            files = {"file": ("test.jpg", f, "image/jpeg")}
            data = {"lot_size": "2", "trailing_sl_points": "10"}
            r = await client.post("http://localhost:8000/api/signal/image", data=data, files=files)
            print("Status:", r.status_code)
            print("Body:", r.text)

asyncio.run(test())
