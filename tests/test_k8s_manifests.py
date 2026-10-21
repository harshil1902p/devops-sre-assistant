"""Static policy checks on k8s/ (no cluster): read-only RBAC, hardened pod spec."""

from pathlib import Path

import yaml

K8S = Path(__file__).resolve().parents[1] / "k8s"
DOCS = [
    d for f in sorted(K8S.glob("*.yaml")) if f.name != "kustomization.yaml" for d in yaml.safe_load_all(f.read_text())
]


def by_kind(kind):
    return [d for d in DOCS if d and d["kind"] == kind]


def test_kustomization_lists_every_manifest():
    listed = set(yaml.safe_load((K8S / "kustomization.yaml").read_text())["resources"])
    assert listed == {f.name for f in K8S.glob("*.yaml")} - {"kustomization.yaml"}


def test_agent_rbac_is_read_only_and_never_touches_secrets():
    for role in by_kind("Role") + by_kind("ClusterRole"):
        for rule in role["rules"]:
            assert set(rule["verbs"]) <= {"get", "list", "watch"}, rule
            assert not {"secrets", "pods/exec", "*"} & set(rule["resources"]), rule


def test_pod_is_hardened():
    (dep,) = by_kind("Deployment")
    spec = dep["spec"]["template"]["spec"]
    assert spec["securityContext"]["runAsNonRoot"] is True
    for c in spec["containers"]:
        sc = c["securityContext"]
        assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"]["drop"] == ["ALL"]
        assert c["resources"]["limits"]["memory"] and {"livenessProbe", "readinessProbe"} <= set(c)
