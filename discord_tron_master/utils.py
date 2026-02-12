import json, os, shutil, logging
from discord_tron_master.classes import log_format
from OpenSSL import crypto


def generate_config_file(filename, data):
    output = json.dumps(data, indent=4)
    logging.info(f"Generated client TLS details:\n" + str(output))
    with open(filename, "w") as f:
        json.dump(data, f, indent=4)


def generate_self_signed_cert(cert_file, key_file, common_name="localhost"):
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)

    cert = crypto.X509()
    cert.get_subject().CN = common_name
    cert.set_serial_number(1000)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(10 * 365 * 24 * 60 * 60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)
    cert.sign(k, "sha256")

    with open(cert_file, "wb") as f:
        f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert))

    with open(key_file, "wb") as f:
        f.write(crypto.dump_privatekey(crypto.FILETYPE_PEM, k))


def copy_cert_to_client(cert_file, destination):
    shutil.copy(cert_file, destination)


def create_tls_cert():
    dir_path = os.path.dirname(os.path.realpath(__file__))
    cert_file = "server_cert.pem"
    key_file = "server_key.pem"
    client_cert_destination = dir_path + "/config/certs"

    generate_self_signed_cert(cert_file, key_file)
    copy_cert_to_client(cert_file, client_cert_destination)


create_tls_cert()  # Call this function to generate the TLS cert and key
