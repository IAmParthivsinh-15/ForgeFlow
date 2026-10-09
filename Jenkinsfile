// CI for ForgeFlow itself (spec sections 108, 123):
//   lint + type check + tests -> frontend build -> images -> (optional) push -> (optional) GitOps
//
// The GitOps stage only updates image tags in k8s/overlays/gitops and pushes that commit;
// Argo CD (manual sync, k8s/argocd/application.yaml) deploys it after a person approves.
// Pushing needs explicit opt-in (PUSH_IMAGES / UPDATE_GITOPS) and Jenkins credentials:
//   registry-credentials  (username/password for REGISTRY)
//   gitops-git            (username + GitHub token allowed to push to the repository)
pipeline {
  agent any
  options {
    timestamps()
    timeout(time: 45, unit: 'MINUTES')
    disableConcurrentBuilds()
  }
  parameters {
    string(name: 'REGISTRY', defaultValue: '', description: 'e.g. ghcr.io/<owner> (empty = local images only)')
    booleanParam(name: 'PUSH_IMAGES', defaultValue: false, description: 'Push images to REGISTRY')
    booleanParam(name: 'UPDATE_GITOPS', defaultValue: false, description: 'Commit new image tags for Argo CD')
  }
  environment {
    IMAGE_TAG = "${env.GIT_COMMIT ? env.GIT_COMMIT.take(12) : 'dev'}"
    UV_LINK_MODE = 'copy'
  }
  stages {
    stage('Lint and type check') {
      steps {
        sh 'uv sync --frozen'
        sh 'uv run ruff check src tests'
        sh 'uv run ruff format --check src tests'
        sh 'uv run mypy'
      }
    }
    stage('Test') {
      steps {
        sh 'uv run pytest -q --junitxml=reports/pytest.xml'
      }
      post {
        always { junit allowEmptyResults: true, testResults: 'reports/pytest.xml' }
      }
    }
    stage('Frontend') {
      steps {
        dir('apps/frontend') {
          sh 'npm ci --no-audit --no-fund'
          sh 'npm run build'
        }
      }
    }
    stage('Images') {
      steps {
        script {
          def prefix = params.REGISTRY ? "${params.REGISTRY}/" : ''
          env.PYTHON_IMAGE = "${prefix}forgeflow-python:${env.IMAGE_TAG}"
          env.FRONTEND_IMAGE = "${prefix}forgeflow-frontend:${env.IMAGE_TAG}"
        }
        sh 'docker build -f docker/python.Dockerfile -t "$PYTHON_IMAGE" .'
        sh 'docker build -f docker/frontend.Dockerfile -t "$FRONTEND_IMAGE" .'
      }
    }
    stage('Push images') {
      when { expression { params.PUSH_IMAGES && params.REGISTRY } }
      steps {
        withCredentials([usernamePassword(credentialsId: 'registry-credentials',
                                          usernameVariable: 'REG_USER', passwordVariable: 'REG_PASS')]) {
          sh 'echo "$REG_PASS" | docker login "${REGISTRY%%/*}" -u "$REG_USER" --password-stdin'
          sh 'docker push "$PYTHON_IMAGE" && docker push "$FRONTEND_IMAGE"'
        }
      }
    }
    stage('GitOps') {
      when { allOf { branch 'main'; expression { params.UPDATE_GITOPS && params.PUSH_IMAGES } } }
      steps {
        dir('k8s/overlays/gitops') {
          sh 'kustomize edit set image forgeflow/python="$PYTHON_IMAGE" forgeflow/frontend="$FRONTEND_IMAGE"'
        }
        withCredentials([usernamePassword(credentialsId: 'gitops-git',
                                          usernameVariable: 'GIT_USER', passwordVariable: 'GIT_TOKEN')]) {
          // The token is read by a credential helper from the environment, never put in a URL.
          sh '''
            git config user.name "ForgeFlow CI"
            git config user.email "ci@forgeflow.local"
            git add k8s/overlays/gitops/kustomization.yaml
            git commit -m "gitops: deploy ${IMAGE_TAG} [skip ci]" || exit 0
            git -c credential.helper='!f() { echo username=$GIT_USER; echo password=$GIT_TOKEN; }; f' \
              push origin HEAD:main
          '''
        }
      }
    }
  }
}
