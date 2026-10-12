import asyncio

from asyncua import ua

from kepclone.config import ChannelConfig, Config, ServerConfig
from kepclone.opcua_server import OpcUaServer
from tests.test_gateway import free_port


def test_secure_endpoint_creates_certificate(tmp_path):
    async def scenario():
        cfg = Config(
            ServerConfig(
                endpoint=f"opc.tcp://127.0.0.1:{free_port()}/kepclone",
                security=["Basic256Sha256_SignAndEncrypt"],
                anonymous=False,
            ),
            [ChannelConfig("L", "logix", [])],
            tmp_path,
        )
        server = OpcUaServer(cfg)
        await server.start()
        try:
            endpoints = await server.server.get_endpoints()
        finally:
            await server.stop()
        return endpoints

    endpoints = asyncio.run(scenario())
    assert (tmp_path / "pki" / "server_cert.der").is_file()
    assert (tmp_path / "pki" / "server_key.pem").is_file()
    assert {e.SecurityMode for e in endpoints} == {ua.MessageSecurityMode.SignAndEncrypt}
    tokens = {p.TokenType for e in endpoints for p in e.UserIdentityTokens}
    assert ua.UserTokenType.Anonymous not in tokens
    assert ua.UserTokenType.UserName in tokens
