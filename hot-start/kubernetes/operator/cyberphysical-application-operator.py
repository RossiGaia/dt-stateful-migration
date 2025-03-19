import kopf, requests, json, random, yaml, time, datetime
from kubernetes import client
from kubernetes.utils import create_from_dict
from k8s_utils import delete_from_dict
from graph_utils import generate_chart


@kopf.on.create("cyberphysicalapplications")
def create_fn(spec, name, logger, **kwargs):
    k8s_client = client.ApiClient()
    k8s_client_custom_object = client.CustomObjectsApi()

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
            service_name = config.get("metadata").get("name")
            service_port = config.get("spec").get("ports")[0].get("targetPort")

            virtualservice_template = open("virtualservice-template.yaml", "rt").read()
            text = virtualservice_template.format(name="test-vs", host=service_name, port=service_port, app_name=name)
            data = yaml.safe_load(text)
            kopf.adopt(data)

            group = "networking.istio.io"
            version = "v1"
            namespace = "default"
            plural = "virtualservices"
            k8s_client_custom_object.create_namespaced_custom_object(group, version, namespace, plural, body=data)

        kopf.label(config, {"related-to": f"{name}"})
        kopf.adopt(config)
        try:
            create_from_dict(k8s_client, config)
        except Exception as e:
            logger.exception("Exception in object creation.")

    return {
        "child-deployment-namespace": deployment_namespace,
        "child-deployment-app-name": deployment_app_name,
        "child-deployment-prometheus-url": deployment_prometheus_url,
        "child-deployment-affinity": deployment_affinity
    }

def get_prometheus_odte(prometheus_url, twin_of, logger):
    query_url = f"{prometheus_url}/api/v1/query?query=odte[pt=\"{twin_of}\"]"
    query_url = query_url.replace("[", "{").replace("]", "}")
    logger.debug(query_url)

    odte = None
    try:
        resp = requests.get(query_url)
    except:
        logger.info("Prometheus not available.")
        raise Exception
    
    try:
        odte = float(json.loads(resp.text)["data"]["result"][0]["value"][1])
    except:
        logger.info("ODTE not available.")
        raise Exception
    
    return odte

def choose_next_deployment(deployments, current_deployment_affinity):
    next_depl_index = random.randint(0, len(deployments) - 1)
    next_depl_affinity = deployments[next_depl_index].get("affinity")

    while next_depl_affinity == current_deployment_affinity:
        next_depl_index = random.randint(0, len(deployments) - 1)
        next_depl_affinity = deployments[next_depl_index].get("affinity")

    return deployments[next_depl_index]

def ensure_pod_termination(k8s_core_v1, app_name, namespace, logger):
    terminated = False
    label_selector = f'app={app_name}'
    while not terminated:
        try:
            resp = k8s_core_v1.list_namespaced_pod(namespace, label_selector=label_selector)
            if len(resp.items) == 0:
                terminated = True
        except:
            logger.info("Cannot list pods.")

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
def migrate_fn(spec, status, name, old, new, logger, **_):

    # guard condition for creation
    if old is None:
        return
    
    k8s_client = client.ApiClient()
    k8s_client_custom_object = client.CustomObjectsApi()

    # trigger a migration
    if old == False and new == True:
        timestamps = {}

        # create new instance
        deployments = spec.get("deployments")
        current_deployment_affinity = status.get("create_fn").get("child-deployment-affinity")
        next_deployment = choose_next_deployment(deployments, current_deployment_affinity)
        next_deployment_configs = next_deployment.get("configs") 
        next_deployment_affinity = next_deployment.get("affinity")

        for config in next_deployment_configs:
            if config.get("kind") == "Deployment":
                config["spec"]["template"]["spec"].update({"nodeSelector": {"zone" : f"{next_deployment_affinity}"}})                        
                next_deployment_namespace = config.get("metadata").get("namespace")
                status["create_fn"]["child-deployment-namespace"] = next_deployment_namespace
                status["create_fn"]["child-deployment-app-name"] = config.get("metadata").get("labels").get("app")
                status["create_fn"]["child-deployment-prometheus-url"] = config.get("spec").get("template").get("metadata").get("annotations").get("prometheusUrl")
                status["create_fn"]["child-deployment-affinity"] = next_deployment_affinity
            
            if config.get("kind") == "Service":
                service_name = config.get("metadata").get("name")
                service_port = config.get("spec").get("ports")[0].get("targetPort")

            kopf.adopt(config)
            kopf.label(config, {"related-to": f"{name}"})
            try:
                create_from_dict(k8s_client, config)
            except:
                logger.exception("Exception creating new object.")
        
        timestamps["Creating new instance"] = datetime.datetime.now()

        # update virtual service for mirroring
        group = "networking.istio.io"
        version = "v1"
        namespace = "default"
        plural = "virtualservices"
        label_selector = f"related-to={name}"
        virtual_service = k8s_client_custom_object.list_namespaced_custom_object(group, version, namespace, plural, label_selector=label_selector).get("items")[0]
        vs_name = virtual_service.get("metadata").get("name")
        vs_http_rule = virtual_service["spec"]["http"]
        vs_http_rule_new = []
        for rule in vs_http_rule:
            rule.update({
                "mirror": {
                    "host": f"{service_name}",
                    "port": {
                        "number": service_port
                    }
                }
            })
            rule.update({
                "mirrorPercentage": {
                    "value": 100.0
                }
            })
            vs_http_rule_new.append(rule)
        virtual_service["spec"]["http"] = vs_http_rule_new
        k8s_client_custom_object.patch_namespaced_custom_object(group, version, namespace, plural, vs_name, body=virtual_service)

        timestamps["Adding mirroring rule"] = datetime.datetime.now()
        
        # wait for the time window
        mirror_time = spec.get("mirrorTime")
        time.sleep(mirror_time)

        timestamps["Waiting for the time window"] = datetime.datetime.now()

        # update virtualservice so it does not mirror
        # change destination host to new deployment service
        virtual_service = k8s_client_custom_object.list_namespaced_custom_object(group, version, namespace, plural, label_selector=label_selector).get("items")[0]
        vs_http_rule_new = []
        for rule in vs_http_rule:
            rule.pop("mirror", None)
            rule.pop("mirrorPercentage", None)
            routes = rule.get("route")
            for route in routes:
                route.update({
                    "destination": {
                        "host": service_name
                    }
                })
            
            vs_http_rule_new.append(rule)
        virtual_service["spec"]["http"] = vs_http_rule_new
        k8s_client_custom_object.patch_namespaced_custom_object(group, version, namespace, plural, vs_name, body=virtual_service)
        
        timestamps["Update virtual service for handoff"] = datetime.datetime.now()

        # delete old instance
        for deployment in deployments:
            if current_deployment_affinity == deployment.get("affinity"):
                configs = deployment.get("configs")
                for config in configs:
                    delete_from_dict(k8s_client, config)

        timestamps["Delete old instance"] = datetime.datetime.now()

        generate_chart(timestamps)
        
        return