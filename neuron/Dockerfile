FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl socat && rm -rf /var/lib/apt/lists/*
# The playback adapter is specific to the browser bundle in this release.
RUN set -eux; \
    repo=https://apt.repos.neuron.amazonaws.com/pool/main/a; \
    curl -fSL --retry 3 "$repo/aws-neuronx-tools/aws-neuronx-tools_2.33.38.0-f874d5618_amd64.deb" -o /tmp/tools.deb; \
    curl -fSL --retry 3 "$repo/aws-neuronx-runtime-lib/aws-neuronx-runtime-lib_2.35.33.0-d2725ccb8_amd64.deb" -o /tmp/runtime.deb; \
    printf '%s\n' 'dd64d69dcf4e25806d805e580ec6d3a67d64d6563b426d89aedb1cb88356347c  /tmp/tools.deb' 'bc3afefcce8008e71dae6583de050f3eee0a54b226e95fbfcbcb60c88cf5bbfa  /tmp/runtime.deb' | sha256sum -c -; \
    dpkg-deb -x /tmp/tools.deb /; \
    dpkg-deb -x /tmp/runtime.deb /; \
    rm /tmp/tools.deb /tmp/runtime.deb
ENV LD_LIBRARY_PATH=/opt/aws/neuron/lib
ENV PATH=/opt/aws/neuron/bin:$PATH
# The relays bridge Explorer's container loopback listeners to Docker's host-only port mappings.
CMD ["sh", "-ec", "socat TCP-LISTEN:13011,reuseaddr,fork TCP:127.0.0.1:3011 & socat TCP-LISTEN:13012,reuseaddr,fork TCP:127.0.0.1:3002 & exec neuron-explorer view --port 3011 --data-path /profiles"]
