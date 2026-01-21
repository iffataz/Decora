import decora
from decora import questions_inb
import time
from PIL import Image
import requests
from apiloader import search_load
import pandas as pd




# search_df = search_load(art)
# print(search_df.shape)
# time.sleep(10)

def gen_everything(url_specific, art):

    search_df = search_load(art)

    def metadata_gen(url_inp,k,foreign,questions=questions_inb):
        tokens=[]
        for i in range(len(questions)):
            question =questions[i]
            probs = decora.query(url=url_inp,question= question,k=k,foreign=foreign)
            for hashmap in probs:
                tokens.append(hashmap['answer'])   
        return tokens

    tokens_main =metadata_gen(url_specific,3,True,questions_inb)

    question_string = ''
    for token_ind in range(len(tokens_main)):
        question_string += f'Is this {tokens_main[token_ind]} '
        if not (token_ind == len(tokens_main) - 1):
                question_string += 'and '

    question_string+='?'
    # print(question_string)
    # time.sleep(10)

    cand =[]
    for i in range(7):
        
        
        image_url = search_df.iloc[i]['contextualImageUrl']
        if not image_url:
            image_url = search_df.iloc[i]["mainImageUrl"]
        # print(image_url)
        binary =  metadata_gen(image_url,1,True,[question_string,]) 
        
        if binary[0] =='yes':
            cand.append(i)
    i=0
    send = []
    for el in cand:
        if i ==4:
            break
        send.append([search_df.iloc[el]['name'],search_df.iloc[el]['pipUrl'],search_df.iloc[el]['mainImageUrl']])
        i +=1
    # print(send)
    return send

