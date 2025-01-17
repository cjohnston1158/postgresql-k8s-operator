# Enable Security in your PostgreSQL deployment 

This is part of the [Charmed PostgreSQL Tutorial](/t/charmed-postgresql-k8s-tutorial-overview/9296?channel=14/stable). Please refer to this page for more information and the overview of the content.

## Transport Layer Security (TLS)
[TLS](https://en.wikipedia.org/wiki/Transport_Layer_Security) is used to encrypt data exchanged between two applications; it secures data transmitted over the network. Typically, enabling TLS within a highly available database, and between a highly available database and client/server applications, requires domain-specific knowledge and a high level of expertise. Fortunately, the domain-specific knowledge has been encoded into Charmed PostgreSQL K8s. This means (re-)configuring TLS on Charmed PostgreSQL K8s is readily available and requires minimal effort on your end.

Again, relations come in handy here as TLS is enabled via relations; i.e. by relating Charmed PostgreSQL K8s to the [TLS Certificates Charm](https://charmhub.io/tls-certificates-operator). The TLS Certificates Charm centralises TLS certificate management in a consistent manner and handles providing, requesting, and renewing TLS certificates.


### Configure TLS
Before enabling TLS on Charmed PostgreSQL K8s we must first deploy the `tls-certificates-operator` charm:
```shell
juju deploy tls-certificates-operator --config generate-self-signed-certificates="true" --config ca-common-name="Tutorial CA"
```

Wait until the `tls-certificates-operator` is up and active, use `juju status --watch 1s` to monitor the progress:
```
Model     Controller  Cloud/Region        Version  SLA          Timestamp
tutorial  charm-dev   microk8s/localhost  2.9.42   unsupported  12:18:05+01:00

App                        Version  Status   Scale  Charm                      Channel    Rev  Address         Exposed  Message
postgresql-k8s                      active       2  postgresql-k8s             14/stable  56   10.152.183.167  no
tls-certificates-operator           waiting      1  tls-certificates-operator  stable     22   10.152.183.138  no       installing agent

Unit                          Workload    Agent  Address       Ports  Message
postgresql-k8s/0*             active      idle   10.1.188.206         Primary
postgresql-k8s/1              active      idle   10.1.188.209
tls-certificates-operator/0*  active      idle   10.1.188.212
```
*Note: this tutorial uses [self-signed certificates](https://en.wikipedia.org/wiki/Self-signed_certificate); self-signed certificates should not be used in a production cluster.*

To enable TLS on Charmed PostgreSQL K8s, relate the two applications:
```shell
juju relate postgresql-k8s tls-certificates-operator
```

### Add external TLS certificate
Use `openssl` to connect to the PostgreSQL and check the TLS certificate in use:
```shell
> openssl s_client -starttls postgres -connect 10.1.188.206:5432 | grep Issuer
...
depth=1 C = US, CN = Tutorial CA
verify error:num=19:self-signed certificate in certificate chain
...
```
Congratulations! PostgreSQL is now using TLS certificate generated by the external application `tls-certificates-operator`.


### Remove external TLS certificate
To remove the external TLS and return to the locally generate one, unrelate applications:
```shell
juju remove-relation postgresql-k8s tls-certificates-operator
```

Check the TLS certificate in use:
```shell
> openssl s_client -starttls postgres -connect 10.1.188.206:5432
...
no peer certificate available
---
No client certificate CA names sent
...
```

The Charmed PostgreSQL K8s application is not using TLS anymore.