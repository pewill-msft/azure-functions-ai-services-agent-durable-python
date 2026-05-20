import azure.functions as func
import azure.durable_functions as df
import logging
import requests
import os
import json
import time
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AzureFunctionBinding,
    AzureFunctionDefinition,
    AzureFunctionDefinitionFunction,
    AzureFunctionStorageQueue,
    AzureFunctionTool,
    PromptAgentDefinition,
)
from azure.core.exceptions import HttpResponseError
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from azure.storage.queue import QueueClient, BinaryBase64EncodePolicy, BinaryBase64DecodePolicy
from openai import AzureOpenAI
from datetime import datetime, timedelta

_aoai_client = None

def get_aoai_client() -> AzureOpenAI:
    """Return a singleton AzureOpenAI client authenticated via AAD."""
    global _aoai_client
    if _aoai_client is None:
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
        )
        _aoai_client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            azure_ad_token_provider=token_provider,
            api_version="2024-10-21",
        )
    return _aoai_client

# Initialize the Durable Functions app with anonymous HTTP authentication level
app = df.DFApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# Name of the queues to get and send the function call messages
input_queue_name = "input"
output_queue_name = "output"

# Stable agent name; the v2 SDK creates new immutable versions under the same name.
AGENT_NAME = "azure-function-agent-summarize-github-issues"

_project_client = None

def get_project_client() -> AIProjectClient:
    """Return a singleton AIProjectClient bound to the Foundry project endpoint."""
    global _project_client
    if _project_client is None:
        _project_client = AIProjectClient(
            endpoint=os.environ["PROJECT_ENDPOINT"],
            credential=DefaultAzureCredential(),
            allow_preview=True,
        )
    return _project_client

def initialize_client():
    """
    Ensure the v2 Foundry agent (with the AzureFunction storage-queue tool) exists,
    and return a project client plus an OpenAI client bound to that agent.
    """
    project = get_project_client()

    # Storage queue service endpoint used to bridge Agent <-> Azure Function
    storage_service_endpoint = os.environ.get("STORAGE_CONNECTION__queueServiceUri")

    azure_function_tool = AzureFunctionTool(
        azure_function=AzureFunctionDefinition(
            function=AzureFunctionDefinitionFunction(
                name="GitHubIssuesSummaries",
                description="Provide a summary of the GitHub issues for the organization within a specified time period.",
                parameters={
                    "type": "object",
                    "properties": {
                        "organization": {
                            "type": "string",
                            "description": "The organization to find GitHub issues for.",
                        },
                        "time": {
                            "type": "string",
                            "description": "The specific time period for which the user is querying GitHub issues.",
                        },
                    },
                    "required": ["time"],
                },
            ),
            input_binding=AzureFunctionBinding(
                storage_queue=AzureFunctionStorageQueue(
                    queue_name=input_queue_name,
                    queue_service_endpoint=storage_service_endpoint,
                ),
            ),
            output_binding=AzureFunctionBinding(
                storage_queue=AzureFunctionStorageQueue(
                    queue_name=output_queue_name,
                    queue_service_endpoint=storage_service_endpoint,
                ),
            ),
        ),
    )

    definition = PromptAgentDefinition(
        model=os.environ["AGENT_MODEL_DEPLOYMENT_NAME"],
        instructions=(
            "You are a GitHub issues assistant. Use the GitHubIssuesSummaries"
            " tool to answer the user's question and present the result as a"
            " concise summary grouped by repository."
        ),
        tools=[azure_function_tool],
    )

    # Create a new version under the stable agent name. If the agent doesn't yet
    # exist this also creates it. Versions are immutable so this is safe to call
    # on every cold start.
    try:
        agent_version = project.agents.create_version(
            agent_name=AGENT_NAME,
            definition=definition,
        )
        logging.info(
            f"Created agent version: {AGENT_NAME} (version {getattr(agent_version, 'version', '?')})"
        )
    except HttpResponseError as e:
        # If a race / duplicate create happens, fall back to the latest existing version.
        logging.warning(f"create_version returned {e.status_code}; using existing agent: {e.message}")

    openai_client = project.get_openai_client(agent_name=AGENT_NAME)
    return project, openai_client

@app.function_name(name="GitHubIssuesSummaries")
@app.durable_client_input(client_name="client")
@app.queue_trigger(arg_name="msg", queue_name="input", connection="STORAGE_CONNECTION")  
async def process_queue_message(msg: func.QueueMessage, client) -> None:
    """
    Function to start orchestration when a message is received in the queue.
    """
    logging.info('Python queue trigger function processed a queue item')

    messagepayload = json.loads(msg.get_body().decode('utf-8'))

    instance_id = await client.start_new("SummarizeGitHubIssues", None, messagepayload)

    logging.info(f'Started orchestration with ID = {instance_id}')
    
@app.function_name(name="SummarizeGitHubIssues")
@app.orchestration_trigger(context_name="context")
def summarize_github_issues(context: df.DurableOrchestrationContext):
    """
    Orchestrator function to summarize GitHub issues.
    """
    first_retry_interval_in_milliseconds = 5000
    max_number_of_attempts = 3
    retry_options = df.RetryOptions(first_retry_interval_in_milliseconds, max_number_of_attempts)
    
    messagepayload = context.get_input()
    correlation_id = messagepayload['CorrelationId']
    function_args = messagepayload.get('function_args', {})
    organization = function_args.get('organization') or messagepayload.get('organization')
    repo = function_args.get('repo') or messagepayload.get('repo')
    prompt_time = function_args.get('time') or messagepayload.get('time')

    # Initialize the time dictionary with actual values
    time = {
        "current_date_time": datetime.utcnow().isoformat() + 'Z',
        "prompt_time": prompt_time
    }

    # Query repositories for the organization using an activity function
    if organization:
        repos = yield context.call_activity_with_retry("QueryRepos", retry_options, organization)
        repo_names = repos["repositories"]
    else:
        prompt = f"Return *only* the GitHub organization name for the repository: {repo}"
        organization = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
        repo_names = [repo]

    # Correctly format the prompt string using f-string
    prompt = f"Assume the current time is {time['current_date_time']}. Convert the following time to ISO format and return *only* the converted time in the format 'YYYY-MM-DDTHH:MM:SSZ': {time['prompt_time']}"

    # Convert time to ISO format using an activity function
    converted_time = yield context.call_activity_with_retry("AskAOAI", retry_options, prompt)
    
    # Fan-out: Create a list of tasks to get issues for each repository
    tasks = []
    
    for repo_name in repo_names:
        task = context.call_activity_with_retry("QueryNewIssues", retry_options, {'repoName': repo_name, 'orgName': organization, 'time': converted_time})
        tasks.append(task)

    # Fan-in: Wait for all tasks to complete and aggregate the results
    all_issues = yield context.task_all(tasks)

    # Ensure all_issues is initialized before filtering
    if all_issues is None:
        all_issues = []

    # Filter out empty arrays of objects
    allIssues = filter_empty_issues(all_issues)

    # Call the activity function to generate a summary using Azure OpenAI
    summary = yield context.call_activity_with_retry("SummarizeIssues", retry_options, allIssues)

    logging.info(f"summary: {summary}")

    # Send message to queue. Sends a mock message for the weather
    result_message = {
        'Value': summary,
        'CorrelationId': correlation_id
    }

    # Queue to send message to
    queue_client = QueueClient(
        os.environ["STORAGE_CONNECTION__queueServiceUri"],
        queue_name="output",
        credential=DefaultAzureCredential(),
        message_encode_policy=BinaryBase64EncodePolicy(),
        message_decode_policy=BinaryBase64DecodePolicy()
    )

    queue_client.send_message(json.dumps(result_message).encode('utf-8'))

@app.function_name(name="QueryRepos")
@app.activity_trigger(input_name='organization')
def get_repos(organization):
    """
    Activity function to get repositories.
    """
    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/orgs/{organization}/repos"
    headers = {
        "Authorization": f"token {access_token}"
    }

    repo_names = []
    page = 1

    while True:
        response = requests.get(url, headers=headers, params={'page': page, 'per_page': 100})
        
        if response.status_code == 200:
            repos = response.json()
            if not repos:
                break
            for repo in repos:
                repo_names.append(repo['name'])
            page += 1
        else:
            print(f"Failed to retrieve repositories: {response.status_code}")
            break

    result = {
        "repositories": repo_names
    }

    return result

@app.function_name(name="AskAOAI")
@app.activity_trigger(input_name='prompt')
def ask_llm(prompt: str):
    """
    Activity function that asks Azure OpenAI a question via chat completions.
    """
    logging.info("in AskAOAI activity")
    client = get_aoai_client()
    completion = client.chat.completions.create(
        model=os.environ["CHAT_MODEL_DEPLOYMENT_NAME"],
        messages=[{"role": "user", "content": prompt}],
    )
    content = completion.choices[0].message.content
    logging.info(content)
    return content
   
@app.function_name(name="QueryNewIssues")
@app.activity_trigger(input_name='queryDetails')
def get_issues(queryDetails):
    """
    Activity function to get issues for a repository.
    """
    organization = queryDetails['orgName']
    repoName = queryDetails['repoName']
    since = queryDetails['time']

    access_token = os.environ.get("GITHUB_ACCESS_TOKEN")

    url = f"https://api.github.com/repos/{organization}/{repoName}/issues"

    headers = {
        "Authorization": f"token {access_token}"
    }
    
    params = {
        "since": since,
    }

    response = requests.get(url, headers=headers, params=params)
    issues = []

    if response.status_code == 200:
        issues_data = response.json()
        if issues_data:
            repo_issues = []
            for issue in issues_data:
                filtered_issue = {
                    "state": issue.get("state"),
                    "user": issue.get("user", {}).get("login"),
                    "title": issue.get("title"),
                    "body": issue.get("body")
                }
                repo_issues.append(filtered_issue)
            issues.append({repoName: repo_issues})
    else:
        print(f"Failed to retrieve issues for {repoName}: {response.status_code}")

    return issues

@app.function_name(name="SummarizeIssues")
@app.activity_trigger(input_name='allIssues')
def summarize_issues(allIssues):
    """
    Activity function to generate a summary using Azure OpenAI chat completions.
    """
    logging.info("in SummarizeIssues activity")
    client = get_aoai_client()
    completion = client.chat.completions.create(
        model=os.environ["CHAT_MODEL_DEPLOYMENT_NAME"],
        messages=[{"role": "user", "content": f"Generate a summary of the following GitHub issues and determine: {allIssues}"}],
        max_tokens=1000,
    )
    return completion.choices[0].message.content

def filter_empty_issues(allIssues):
    """
    Function to filter out empty arrays of objects.
    """
    return [issue for issue in allIssues if issue]

@app.route(route="prompt", auth_level=func.AuthLevel.ANONYMOUS)
def prompt(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP trigger function to handle prompts and interact with the agent.
    """
    logging.info('Python HTTP trigger function processed a request.')

    origin = req.headers.get('Origin', '')
    allowed_origins = [
        "http://localhost:3000",
        "https://wonderful-wave-07c299e1e.6.azurestaticapps.net",
        "https://stapp-web-5som3lu6awirw.azurestaticapps.net",
        "https://icy-flower-08b6bcf03.7.azurestaticapps.net"
    ]
    cors_origin = origin if origin in allowed_origins else "*"

    if req.method == "OPTIONS":
        return func.HttpResponse(
            status_code=204,
            headers={
                'Access-Control-Allow-Origin': cors_origin,
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization',
                'Access-Control-Allow-Credentials': 'true',
                'Access-Control-Max-Age': '86400'
            }
        )

    req_body = req.get_json()
    prompt_text = req_body.get('Prompt')

    _project, openai_client = initialize_client()

    answer_text = None
    debug_info = {}
    try:
        response = openai_client.responses.create(
            input=prompt_text,
            parallel_tool_calls=False,
            extra_body={"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}},
        )
        try:
            debug_info = json.loads(response.model_dump_json())
        except Exception:
            debug_info = {"repr": repr(response)[:2000]}
        answer_text = getattr(response, "output_text", None)
        if not answer_text:
            for item in getattr(response, "output", []) or []:
                for part in getattr(item, "content", []) or []:
                    text = getattr(part, "text", None)
                    if text:
                        answer_text = text
                        break
                if answer_text:
                    break
        logging.info(f"Agent response: {answer_text}")
    except Exception as e:
        logging.exception(f"Agent invocation failed: {e}")
        debug_info = {"exception": repr(e)}

    response_message = {
        "message": answer_text if answer_text else "No response generated",
        "debug": debug_info,
    }

    return func.HttpResponse(
        json.dumps(response_message),
        mimetype="application/json",
        headers={
            'Access-Control-Allow-Origin': cors_origin,
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Allow-Credentials': 'true'
        }
    )
