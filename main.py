import aiohttp
import asyncio
import os
import sys
import json
import mysql.connector
from mysql.connector import Error

from pipecat.frames.frames import LLMMessagesFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import LLMUserContextAggregator, LLMAssistantContextAggregator
from pipecat.services.deepgram import DeepgramSTTService, DeepgramTTSService
from pipecat.services.openai import OpenAILLMService, OpenAILLMContext
from pipecat.transports.network.websocket_server import WebsocketServerParams, WebsocketServerTransport
from pipecat.vad.silero import SileroVADAnalyzer
from pipecat.vad.vad_analyzer import VADParams
from loguru import logger
from openai.types.chat import ChatCompletionToolParam

from dotenv import load_dotenv
load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

# Database functions
def create_connection():
    try:
        connection = mysql.connector.connect(
            host=os.getenv('DB_HOST'),
            user=os.getenv('DB_USER'),
            password=os.getenv('DB_PASSWORD'),
            database='dialokxml'
        )
        return connection
    except Error as e:
        logger.error(f"Error connecting to MySQL database: {e}")
        return None

def search_company(name):
    connection = create_connection()
    if connection is None:
        return None

    try:
        cursor = connection.cursor(dictionary=True)
        query = """
        SELECT c.companyid, c.companyname, c.generalinfo,
               d.personid, d.firstname, d.lastname, d.title
        FROM companies c
        LEFT JOIN directory d ON c.companyname = d.company
        WHERE c.companyname LIKE %s
        LIMIT 20
        """
        cursor.execute(query, (f"%{name}%",))
        results = cursor.fetchall()
        return results
    except Error as e:
        logger.error(f"Error searching for company: {e}")
        return None
    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()

def search_person(name, company=None):
    connection = create_connection()
    if connection is None:
        return None

    try:
        cursor = connection.cursor(dictionary=True)
        if company:
            query = """
            SELECT personid, firstname, lastname, title, company, concernid
            FROM directory
            WHERE CONCAT(firstname, ' ', lastname) LIKE %s AND company LIKE %s
            LIMIT 5
            """
            cursor.execute(query, (f"%{name}%", f"%{company}%"))
        else:
            query = """
            SELECT personid, firstname, lastname, title, company, concernid
            FROM directory
            WHERE CONCAT(firstname, ' ', lastname) LIKE %s
            LIMIT 5
            """
            cursor.execute(query, (f"%{name}%",))
        results = cursor.fetchall()
        return results
    except Error as e:
        logger.error(f"Error searching for person: {e}")
        return None
    finally:
        if connection.is_connected():
            cursor.close()
            connection.close()

# AI function calling
async def start_search_person(llm):
    await llm.push_frame(TextFrame("Let me search for that person in our database."))

async def start_search_company(llm):
    await llm.push_frame(TextFrame("I'll look up that company in our records."))

async def ai_search_company(llm, args):
    name = args.get('name', '')
    results = search_company(name)
    if results:
        company_info = {
            "name": results[0]['companyname'],
            "info": results[0]['generalinfo'],
            "employees": []
        }
        for r in results:
            if r['personid']:
                company_info['employees'].append({
                    "name": f"{r['firstname']} {r['lastname']}",
                    "title": r['title'],
                    "personid": r['personid']
                })
        return {"result": company_info}
    return {"error": f"No company found with name '{name}'"}

async def ai_search_person(llm, args):
    name = args.get('name', '')
    company = args.get('company', None)
    results = search_person(name, company)
    if results:
        return {
            "results": [
                {
                    "name": f"{r['firstname']} {r['lastname']}",
                    "title": r['title'],
                    "company": r['company'],
                    "concernid": r['concernid'],
                    "personid": r['personid']
                } for r in results
            ]
        }
    return {"error": f"No person found with name '{name}'" + (f" at company '{company}'" if company else "")}

async def create_application():
    transport = WebsocketServerTransport(
        params=WebsocketServerParams(
            host="0.0.0.0",  # Allow external connections
            port=8765,
            audio_out_enabled=True,
            add_wav_header=True,
            vad_enabled=True,
            vad_analyzer=SileroVADAnalyzer(params=VADParams(
                stop_secs=0.05, start_secs=0.05
            )),
            vad_audio_passthrough=True
        )
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4-0613",
        max_tokens=200,
        temperature=0.5,
    )
    llm.register_function(
        "ai_search_person",
        ai_search_person,
        start_callback=start_search_person
    )
    llm.register_function(
        "ai_search_company",
        ai_search_company,
        start_callback=start_search_company
    )

    async with aiohttp.ClientSession() as session:
        stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))

        tts = DeepgramTTSService(
            aiohttp_session=session,
            api_key=os.getenv("DEEPGRAM_API_KEY"),
        )

        tools = [
            ChatCompletionToolParam(
                type="function",
                function={
                    "name": "ai_search_person",
                    "description": "Search for a person by name and optionally company in the company database",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "The name of the person to search for"
                            },
                            "company": {
                                "type": "string",
                                "description": "The company name to narrow down the search (optional)"
                            }
                        },
                        "required": ["name"]
                    }
                }
            ),
            ChatCompletionToolParam(
                type="function",
                function={
                    "name": "ai_search_company",
                    "description": "Search for a company by name and retrieve its employees",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "The name of the company to search for"
                            }
                        },
                        "required": ["name"]
                    }
                }
            )
        ]

        messages = [
            {
                "role": "system",
                "content": "You are a digital switchboard operator for Line Carrier. You can search for information about companies and their employees in our database. Use the ai_search_company function to find information about companies and their employees. Use the ai_search_person function to find specific individuals, optionally narrowing the search by company name if provided by the caller.",
            },
        ]

        context = OpenAILLMContext(messages, tools)
        tma_in = LLMUserContextAggregator(context)
        tma_out = LLMAssistantContextAggregator(context)

        pipeline = Pipeline([
        transport.input(),
        stt,
        tma_in,
        llm,
        tts,
        transport.output(),
        tma_out
    ])

    task = PipelineTask(
        pipeline,
        PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            report_only_initial_ttfb=True
        ))

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        await tts.say("Thank you for calling Line Carrier. How can I assist you today?")

    runner = PipelineRunner()  # Create the runner here
    return task, runner, transport  # Return the transport as well

async def run_application():
    task, runner, transport = await create_application()
    await runner.run(task)
    return transport  # Return the transport for potential use in the ASGI app

if __name__ == "__main__":
    asyncio.run(run_application())