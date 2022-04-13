import random
import string
import rsa

# A helper class to handle server validation.
#
# The printer connection to OctoEverywhere is established over a secure websocket using the lastest TLS protocls and policies.
# However, since OctoEverywhere handles very senstive access to phyical 3d printers, we want to make sure the connection is very secure.
# No bad actor should ever be able to generate a valid SSL cert for OctoEverywhere. But it would be possible to add a bad root cert to the
# device and then generate certs based on it.
#
# Thus to add another layer of security, we will validate the secure websocket connection is connected to a valid OctoEverywhere server by also
# doing an RSA challenge. We encrypt a random string the client generates with a public key and send it to the server. The server will use it's private key
# to decrypt it and send the plan text challnege back (over the secure websocket). If the server can successfully decrypt our message, it knows the correct private
# key and thus can be trusted.
class ServerAuthHelper:

    # Defines what key we expect to be using
    c_ServerAuthKeyVersion = 1

    # Version 1 of the RSA public key.
    c_ServerPublicKey = b"""-----BEGIN PUBLIC KEY-----\nMIICITANBgkqhkiG9w0BAQEFAAOCAg4AMIICCQKCAgBZ1aJw+M6p88jObamIINij\nppZVnrdSG/dekqmEyEx8hQ6fDnn3zFKEzAYTEEnT7GbgEHthpseobRjT46TVcyDX\n7SJ/1uaEMsgw0aIB55ijo5I9YL588CqBhGvXn4q9pFOn9Z0R/Rdtk5WGGadYTszz\nBSXprJ13E2ZMgRprIR1rv2mItbrwQ8XmRwtwiH3A6XX7yjQ4hSQt9neTOhwGZG9a\n6IKAHL3OpuWervc8u3CZrUuQE0FXDD5OCYQ1vOdkSQwsFY89h5hEpAp+kXQCzDGP\nJMbtr+3lXPap91PAHqeD45CDxg5tOL0/ykhVrAywXM+mmuwjn5HzgsseCHNxbn45\nHGJYtA7vDUBbtFdIt250nTDhJpgPyZKKG+We71bUm55C6fCeay8pGGgBZeG25zgk\nuB2CfFMHdwcKTRCykrGh3umKeHc4mUFmJAr90+Fua+CPxksiDGCzjtDSxWDoYkpK\niRPw2UCwAp1g2u3QVcxHjwq1tOfe1+BjDfN2i9f0bjWANsj0OTOlaJH9GBs8vgPe\n1flj39yX9f/HkNyI5QuJL+xRy+/+CSqCBUW+y+eU7g6HCpyrMBcP0amxKHKFCoOJ\nDrHpT25eNCZWXMQYPvViW/uVH2UiPgRBxeSXUvWrUOUU6x4G+FsNhcn+cX3XS19t\nq1Lnq5bTwPiTNrsY6og53wIDAQAB\n-----END PUBLIC KEY-----\n"""

    # Defines the length of the challenge we will encrypt.
    c_ServerAuthChallengeLength = 64

    def __init__(self, logger):
        self.Logger = logger

        # Generate our random challenge string.
        self.Challenge =  ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(ServerAuthHelper.c_ServerAuthChallengeLength))

    # Returns a string that is our challenge encrypted with the public RSA key.
    def GetEncryptedChallenge(self):
        try:
            publicKey = rsa.PublicKey.load_pkcs1_openssl_pem(ServerAuthHelper.c_ServerPublicKey)
            return rsa.encrypt(self.Challenge.encode('utf8'), publicKey)
        except Exception as e:
            self.Logger.error("GetEncryptedChallenge failed.  "+str(e))
        return None

    # Validates the decrypted challenge the server returned is correct.
    def ValidateChallengResponse(self, response):
        if response is None:
            return False
        if response != self.Challenge:
            return False
        return True
