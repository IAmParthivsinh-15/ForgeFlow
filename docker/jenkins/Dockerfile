# Local Jenkins for ForgeFlow CI (spec section 56). Configured as code; no setup wizard.
FROM jenkins/jenkins:lts-jdk21

USER root
# Toolchains the generated pipelines need for the repositories' own commands.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python-is-python3 \
       python3-pip nodejs npm git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'
USER jenkins

COPY plugins.txt /usr/share/jenkins/ref/plugins.txt
RUN jenkins-plugin-cli --plugin-file /usr/share/jenkins/ref/plugins.txt

COPY casc.yaml /usr/share/jenkins/ref/casc.yaml
ENV CASC_JENKINS_CONFIG=/usr/share/jenkins/ref/casc.yaml \
    JAVA_OPTS="-Djenkins.install.runSetupWizard=false"
