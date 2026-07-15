# Egress firewall for the matrix-admin-agent. Tools are baked in so a restart re-locks
# the (possibly already-locked) shared netns without needing network access for apk.
FROM alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
RUN apk add --no-cache iptables ip6tables bind-tools
COPY egress-fw.sh /usr/local/bin/egress-fw.sh
RUN chmod +x /usr/local/bin/egress-fw.sh
ENTRYPOINT ["/usr/local/bin/egress-fw.sh"]
