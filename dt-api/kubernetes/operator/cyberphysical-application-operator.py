import kopf, requests, json, random, datetime, os
from kubernetes import client
from kubernetes.utils import create_from_dict
from k8s_utils import delete_from_dict
from graph_utils import generate_chart

CLUSTER_IP = os.environ.get("CLUSTER_IP")

if not CLUSTER_IP:
    print("CLUSTER_IP env var not present. Exiting.")
    exit(1)

@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, namespace, meta, logger, **kwargs):
    k8s_client = client.ApiClient()
    k8s_custom_object = client.CustomObjectsApi()

    deployments = spec.get("deployments")
    preferred_affinity = spec.get("requirements").get("preferredAffinity")

    deployment_affinity = None
    deployment_configs = None
    deployment_namespace = None
    deployment_app_name = None
    deployment_prometheus_url = None

    if preferred_affinity == "" or preferred_affinity is None:
        deployment_configs = deployments[0].get("configs")
        deployment_affinity = deployments[0].get("affinity")
    
    else:
        for deployment in deployments:
            deployment_affinity = deployment.get("affinity")
            if deployment_affinity == preferred_affinity:
                deployment_configs = deployment.get("configs")
                break
            
    for config in deployment_configs:
        if config.get("kind") == "Deployment":
            config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{deployment_affinity}"}})
            deployment_namespace = config.get("metadata").get("namespace")
            deployment_app_name = config.get("metadata").get("labels").get("app")
            deployment_prometheus_url = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
        
        if config.get("kind") == "Service":
            kopf.label(config, {"debug": "current-service"})

        kopf.label(config, {"related-to": f"{name}"})
        kopf.adopt(config)
        try:
            create_from_dict(k8s_client, config)
        except Exception as e:
            logger.exception("Exception in object creation.")

    annotations_patch = {"metadata": {"annotations": dict(meta.annotations)}}
    annotations_patch["metadata"]["annotations"]["child-deployment-namespace"] = deployment_namespace
    annotations_patch["metadata"]["annotations"]["child-deployment-app-name"] = deployment_app_name
    annotations_patch["metadata"]["annotations"]["child-deployment-app-name"] = deployment_prometheus_url
    annotations_patch["metadata"]["annotations"]["child-deployment-affinity"] = deployment_affinity


    group = "test.dev"
    version = "v1"
    plural = "cyberphysicalapplications"
    resp = k8s_custom_object.patch_namespaced_custom_object(group, version, namespace, plural, name, body=annotations_patch)


def get_prometheus_odte(prometheus_url, twin_of, logger):
    """Fetch ODTE value from Prometheus."""

    query_url = f"{prometheus_url}/api/v1/query?query=odte[pt=\"{twin_of}\"]".replace("[", "{").replace("]", "}")
    logger.debug(query_url)

    odte = None
    try:
        resp = requests.get(query_url)
        odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
        return odte
    
    except Exception as e:
        logger.warn(f"Prometheus not available. {e}")
        raise Exception
    
def choose_next_deployment(deployments, current_deployment_affinity):
    """Select a new deployment avoiding the current one."""

    available_deployments = [d for d in deployments if d.get("affinity") != current_deployment_affinity]
    return random.choice(available_deployments) if available_deployments else None

def ensure_pods_ready(k8s_core_v1, app_name, namespace, logger):
    """Wait until all pods of the deployment are ready."""
    
    label_selector = f'app={app_name}'
    try:
        resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
    except:
        logger.error("Cannot list pods.")
        return
    
    pods = resp.items

    for pod in pods:
        pod_ready = False
        pod_name = pod.metadata.name
        
        while not pod_ready:
            try:
                pod_status = k8s_core_v1.read_namespaced_pod_status(pod_name, namespace)
            except:
                logger.error("Cannot read pod status.")

            conditions = pod_status.status.conditions
            for condition in conditions:
                if condition.type == "Ready" and condition.status == "True":
                    pod_ready = True

@kopf.on.update("cyberphysicalapplications")
def update_fn(name, spec, namespace, **kwargs):
    
    k8s_client_custom_object = client.CustomObjectsApi()
    migrate_field = spec.get("migrate", None)
    if migrate_field:
        cpa_patch = {
            "spec": {
                "migrate": migrate_field
            }
        }
        group = 'test.dev'
        version = 'v1'
        plural = 'cyberphysicalapplications'
        obj = k8s_client_custom_object.patch_namespaced_custom_object(group, version, namespace, plural, name, body=cpa_patch)

@kopf.on.field("cyberphysicalapplications", field="spec.migrate")
def migrate_fn(spec, namespace, meta, name, old, new, logger, **_):

    # guard condition for creation
    if old is None:
        return
    
    k8s_client = client.ApiClient()
    k8s_core_v1 = client.CoreV1Api()
    k8s_custom_object = client.CustomObjectsApi()

    # trigger a migration
    if old == False and new == True:
        timestamps = {}

        # create new instance
        deployments = spec.get("deployments")
        current_deployment_affinity = meta.get("annotations").get("child-deployment-affinity")
        current_deployment_namespace = meta.get("annotations").get("child-deployment-namespace")
        next_deployment = choose_next_deployment(deployments, current_deployment_affinity)
        next_deployment_configs = next_deployment.get("configs") 
        next_deployment_affinity = next_deployment.get("affinity")

        annotations_patch = {"metadata": {"annotations": dict(meta.annotations)}}
        for config in next_deployment_configs:
            if config.get("kind") == "Deployment":
                config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{next_deployment_affinity}"}})                        
                next_deployment_namespace = config.get("metadata").get("namespace")
                next_deployment_app_name = config.get("metadata").get("labels").get("app")
                annotations_patch["metadata"]["annotations"]["child-deployment-namespace"] = next_deployment_namespace
                annotations_patch["metadata"]["annotations"]["child-deployment-app-name"] = config.get("metadata").get("labels").get("app")
                annotations_patch["metadata"]["annotations"]["child-deployment-app-name"] = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                annotations_patch["metadata"]["annotations"]["child-deployment-affinity"] = next_deployment_affinity

            if config.get("kind") == "Service":
                next_deployment_service_name = config.get("metadata").get("name")
                next_deployment_service = config

            kopf.adopt(config)
            kopf.label(config, {"related-to": f"{name}"})
            try:
                create_from_dict(k8s_client, config)
            except:
                logger.exception("Exception creating new object.")
        
        # wait for it to start correctly
        ensure_pods_ready(k8s_core_v1, next_deployment_app_name, next_deployment_namespace, logger)
        print("Deployment\'s pods started.")

        timestamps["Creating new instance"] = datetime.datetime.now()

        # recover the state from the old one freezing the traffic to ensure no change in state
        label_selector = "debug=current-service"
        resp = k8s_core_v1.list_namespaced_service(current_deployment_namespace, label_selector=label_selector)
        current_deployment_service_port = resp.items[0].spec.ports[0].node_port

        resp = k8s_core_v1.read_namespaced_service(next_deployment_service_name, next_deployment_namespace)
        next_deployment_service_port = resp.spec.ports[0].node_port

        service_url = f"http://{CLUSTER_IP}:{current_deployment_service_port}/dump"
        resp = requests.post(service_url)

        # measuring purposes
        json_object = json.dumps(json.loads(resp.text)).encode("UTF-8")
        print(f"Size in megabytes of the received state encoded in UTF-8: {len(json_object) / 1024 / 1024}")
        print(f"No. of messages {len(json.loads(resp.text)["dump"]["messages_deque"])}")

        timestamps["Receiving state"] = datetime.datetime.now()

        # restore the state in the new instance
        next_service_url = f"http://{CLUSTER_IP}:{next_deployment_service_port}/restore"
        headers = {
            "Content-Type": "application/json"
        }

        data = resp.text
        resp = requests.post(next_service_url, data=data, headers=headers)
        print(resp.text)

        timestamps["Restoring state"] = datetime.datetime.now()
        
        # re route traffic to the new instance
        # since it is supposed to use MQTT, no specific rerouting is necessary

        # delete old instance
        for depl in deployments:
            if depl.get("affinity") == current_deployment_affinity:
                for config in depl.get("configs"):
                    delete_from_dict(k8s_client, config)

        timestamps["Deleting old instance"] = datetime.datetime.now()

        kopf.label(next_deployment_service, {"debug": "current-service"})
        resp = k8s_core_v1.patch_namespaced_service(next_deployment_service_name, next_deployment_namespace, next_deployment_service)
        # print(resp)

        group = "test.dev"
        version = "v1"
        plural = "cyberphysicalapplications"
        resp = k8s_custom_object.patch_namespaced_custom_object(group, version, namespace, plural, name, body=annotations_patch)


        generate_chart(timestamps)

        return