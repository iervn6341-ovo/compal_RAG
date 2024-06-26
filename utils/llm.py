import os
import queue
import re
import time
import json
import requests
import gradio as gr
import pandas as pd
from textwrap import dedent
from utils.logging_colors import logger
from utils.qdrant import qdrant_client, embedding_model_list
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser
from langchain.callbacks import StreamingStdOutCallbackHandler

protocal = os.getenv("PROTOCAL", "http")
url = os.getenv("SERVER_URL", "10.20.1.96")
port = os.getenv("PORT", "5001")

# 用來控制多人同時 Submit 要處理的請求佇列，多個請求傳入時最多只接受一個請求
submit_queue = queue.Queue(maxsize=1)
'''用來控制多人同時 Submit 要處理的請求佇列，多個請求傳入時最多只接受一個請求'''


class LLM:
    def get_model_list() -> list:
        try:
            text_model_list = json.loads(requests.get(f"{protocal}://{url}:{port}/v1/internal/model/list", 
                                                      timeout=(10, None)).text)["model_names"]
            except_file = ["wget-log", "output.log", "Octopus-v2", "Phi-3-mini-4k-instruct", "bge-reranker-large"]
            text_model_list = [f for f in text_model_list if f not in except_file and "gguf" not in f]
            logger.info("Model list fetched successfully")
            return text_model_list
        except requests.exceptions.ConnectionError:
            logger.error("ConnectionError: Failed to connect to the server.")
            return ["None"]
        except requests.exceptions.Timeout:
            logger.error("Timeout: The request timed out.")
            return ["None"]

    def send_query(text_dropdown: str, text: str, detail_output_box: list, 
                   summary_output_box: list, embed_model: str, topk: str, score_threshold: float, 
                   temperature: str = "0", prompt: str = "", **kwargs):
        if text_dropdown == '':
            gr.Warning("請選擇一語言模型")
            return False
        
        if embed_model == None:
            gr.Warning("請選擇一詞嵌入模型")
            return False
        elif embed_model not in embedding_model_list:
            gr.Warning("無相關詞嵌入模型")
            return False
        
        if text == '':
            gr.Warning("請輸入問題")
            return False
        
        detail_output_box.append([text, ""])
        summary_output_box.append([text, ""])
        
        if not os.path.exists("./standard_response.csv"):
            pd.DataFrame(columns=["Q", "A(detail)", "A(summary)"]).to_csv("./standard_response.csv", index=False)
        csv = pd.read_csv("./standard_response.csv", on_bad_lines='skip')
        question_list = csv["Q"].values
        for question in question_list:
            if text.strip("?!.。") == question.strip("?!.。"):
                detail_output_box[-1][1] = csv[csv["Q"] == question]["A(detail)"].values[0]
                summary_output_box[-1][1] = csv[csv["Q"] == question]["A(summary)"].values[0]
                yield "", detail_output_box, summary_output_box, gr.update(visible=False)
                return True

        if submit_queue.full():
            gr.Warning("聊天佇列以滿, 請稍後再發送請求。")
            return False
        else:
            model_data = json.load(open("config.json", "r", encoding="utf-8"))["model_config"]
            if re.search(r"gguf", text_dropdown):
                args = model_data["gguf"]
                gr.Info("gguf格式的模型可能因為評估題詞而載入過久,請謹慎使用.")
            elif re.search(r"2b|2B|6b|6B|7b|7B|8b|8B|128k", text_dropdown):
                args = model_data["2&7&8B"]
            elif re.search(r"13b|13B", text_dropdown):
                args = model_data["13B"]
            else:
                logger.error("未找到相關設定檔")
                raise gr.Error("未找到相關設定檔")
            logger.info(f"args get")
            
            logger.info("Loading model...")
            try:    
                response = json.loads(requests.post(
                    f"{protocal}://{url}:{port}/v1/internal/model/load",
                    json={
                        "model_name": text_dropdown,
                        "args": args,
                        "settings": {"instruction_template": "Alpaca"}
                    },
                    timeout=(10, None)
                ).text)
                if response["status"] == 0:
                    logger.info("Model loaded successfully")
                    gr.Info("語言模型讀取成功")
                else: 
                    logger.error("failed to load model")
                    raise gr.Error("讀取模型失敗...")
            except requests.exceptions.Timeout:
                logger.error("Timeout request...")
                raise gr.Error("連線逾時...")
            except requests.exceptions.RequestException as e:
                logger.error("Bad request...")
                raise gr.Error("錯誤...")
        
        
        logger.info(f"{text_dropdown} model ready")
        logger.info("Add the request into queue...")

        detail_api = Chat_api(temperature=float(temperature))
        detail_chain = detail_api.setup_model(search_content=text, topk=topk,
                                              embed_model=embed_model, custom_prompt=prompt,
                                              score_threshold=float(score_threshold))
        submit_queue.put(detail_chain)
        start = time.time()
        temp = ""
        for chunk in detail_chain.stream("#zh-tw " + text):
            temp += chunk
            detail_output_box[-1][1] += chunk
            yield "", detail_output_box, summary_output_box, gr.update(visible=False)
        end = time.time()
        logger.info(f"")
        logger.info(f"[Detail output]: {temp} ,time cost: {end-start}")
        
        summary_api = Chat_api(kwargs.get("temperature", 0), custom_content=detail_output_box[-1][1])
        summary_chain = summary_api.setup_model(search_content=text, topk=topk, 
                                               embed_model=embed_model, score_threshold=float(score_threshold))
        start = time.time()
        temp = ""
        for chunk in summary_chain.stream("#zh-tw " + text):
            temp += chunk
            summary_output_box[-1][1] += chunk
            yield "", detail_output_box, summary_output_box, gr.update(visible=False)
        end = time.time()
        logger.info(f"[Summary output]: {temp} ,time cost: {end-start}")
        
        yield "", detail_output_box, summary_output_box, gr.update(visible=True)
        logger.info("remove the request from queue...")
        submit_queue.get()

    def get_model() -> str | bool:
        try:
            response = json.loads(requests.get(f"{protocal}://{url}:{port}/v1/internal/model/info", 
                                               timeout=(10, None)).text)
            return response["model_name"]
        except requests.exceptions.ConnectionError:
            logger.error("ConnectionError: Failed to connect to the server.")
            gr.Warning("無法連接至伺服器.")
            return False
        except requests.exceptions.Timeout:
            logger.error("Timeout: The request timed out.")
            gr.Warning("連線逾時.")
            return False


class Chat_api:
    """
    is_rag: 是否使用RAG
    temperature: 模型感情
    role: 對話角色
    """
    
    RAG_DETAIL_SYS_PROMPT = dedent("""你必須合併參考資料中的資訊來回答問題，並避免資料混置的問題。

若沒有辦法從以下參考資料中取得資訊或參考資料為空白，則回答"沒有相關資料"，且不須回覆參考檔案名稱及頁碼。

輸出必須使用繁體中文，並且在回答的最後加入參考檔案名稱及頁碼。
        """)
    
    RAG_SUMMARY_SYS_PROMPT = dedent("""
        你是一個客服聊天機器人，請將使用者提供的敘述做summary, 回答越精簡越好, 若提供的內容中有參考資料, 請在回答中加入參考資料檔案的名稱與頁碼，並且用繁體中文回覆。""")
    
    def __init__(self, temperature: float = 0, role: str = "assistant", custom_content: str = ""):
        self.temperature = temperature
        self.role = role
        self.custom_content = custom_content
        
    def setup_model(self, score_threshold: int, embed_model: str, 
                    search_content: str = "", topk: str = "5", custom_prompt: str = "", **kwargs) -> ChatOpenAI:
        if topk == "": 
            topk = "5"
            
        if score_threshold == 0:
            score_threshold = None
        
        qdrant_client.set_model(embed_model, cache_dir="./.cache")
        result = qdrant_client.query(
            collection_name=embed_model.replace("/", "_"),
            query_text=search_content,
            limit=int(topk),
            score_threshold=score_threshold)
        
        content = "\n\n--------------------------\n\n".join(text.metadata["document"] for text in result)

        # debug use
        # print(content)
        
#         result_score_list = []  
#         index = 1
#         for r in result:
#             result_score_list.append(r.score)
#             print(dedent("""
# -----------------------------------
# Index: {}
# Top-K: {}
# Question: {}
# Result: {}
# Score: {}
# -----------------------------------
# """.format(index, topk, search_content, r.document, r.score)
#             ))
#             index+=1
#         print("Scores: {}".format(result_score_list))
        
        if custom_prompt != "":
            PROMPT = custom_prompt
        else:
            if self.custom_content != "":
                PROMPT = self.RAG_SUMMARY_SYS_PROMPT
            else:
                PROMPT = self.RAG_DETAIL_SYS_PROMPT

        prompt_template = f"""{PROMPT}
        
        # 參考資料
        {{context}} 

        # 使用者問題
        Question: {{question}}"""
        
        self.chain = (
            {"context": lambda x: content if self.custom_content == "" else self.custom_content, "question": RunnablePassthrough()}
            | ChatPromptTemplate.from_template(prompt_template)
            | ChatOpenAI(streaming=True, max_tokens=0, temperature=self.temperature,
                        callbacks=[StreamingStdOutCallbackHandler()])
            | StrOutputParser()
        )
        return self.chain
    