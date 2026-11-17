import pymysql

# Use PyMySQL for Django's MySQL backend. mysqlclient's Windows TLS
# handshake fails against TiDB Cloud with SEC_E_DECRYPT_FAILURE.
pymysql.install_as_MySQLdb()
