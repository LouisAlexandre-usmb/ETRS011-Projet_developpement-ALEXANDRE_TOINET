# Agent SNMP de test (Net-SNMP) : simule un équipement réseau supervisable
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends snmpd \
 && rm -rf /var/lib/apt/lists/*

COPY snmpd.conf /etc/snmp/snmpd.conf

EXPOSE 161/udp

# -f : premier plan ; -Lo : logs sur la sortie standard ; -C -c : n'utiliser que notre fichier
CMD ["snmpd", "-f", "-Lo", "-C", "-c", "/etc/snmp/snmpd.conf"]
