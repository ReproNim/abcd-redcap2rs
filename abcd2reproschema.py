import pandas as pd
import os
import re
import argparse
import logging
from langdetect import detect, DetectorFactory
from langdetect.lang_detect_exception import LangDetectException
from reproschema.models import Item, Activity, Protocol, write_obj_jsonld

# Initialize logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

# Fix the seed of the random generator for reproducibility
DetectorFactory.seed = 0

# Define variables for context URL and version
SCHEMA_CONTEXT_URL = "https://raw.githubusercontent.com/ReproNim/reproschema/1.0.0/contexts/generic"
SCHEMA_VERSION = SCHEMA_CONTEXT_URL.split('/')[-3]

def ensure_directory_exists(directory):
    os.makedirs(directory, exist_ok=True)
    logging.debug(f"Ensured directory exists: {directory}")

def extract_variables(js_expression):
    pattern = re.compile(r'\b\w+\b')
    variables = pattern.findall(js_expression)
    logging.debug(f"Extracted variables from jsExpression: {variables}")
    return variables

def update_add_properties(add_properties, variables):
    variable_set = {prop["variableName"] for prop in add_properties}
    for var in variables:
        if var in variable_set:
            for prop in add_properties:
                if prop["variableName"] == var:
                    prop["valueRequired"] = True
                    break
        else:
            add_properties.append({
                "isAbout": f"items/{var.strip()}",
                "variableName": var.strip(),
                "valueRequired": True
            })
    logging.debug(f"Updated addProperties: {add_properties}")
    return add_properties

def read_csv(file_path):
    try:
        logging.debug(f"Reading CSV file: {file_path}")
        return pd.read_csv(file_path, low_memory=False)
    except FileNotFoundError as e:
        logging.error(f"File not found: {file_path}")
        raise e
    except Exception as e:
        logging.error(f"Error reading CSV file: {e}")
        raise e

def filter_domains(dataframe, domain_to_exclude):
    filtered_df = dataframe[dataframe["domain"] != domain_to_exclude]
    logging.debug(f"Filtered domains excluding '{domain_to_exclude}': {filtered_df.shape}")
    return filtered_df

def get_protocol_dfs(dataframe):
    protocol_dfs = [dataframe[dataframe["study"] == study] for study in dataframe["study"].unique()]
    logging.debug(f"Divided into {len(protocol_dfs)} protocol dataframes")
    return protocol_dfs

def parse_notes(notes):
    if pd.isna(notes):
        return {
            "ui": {"inputType": "text"},
            "responseOptions": {"valueType": ["xsd:string"]}
        }
    
    pattern = re.compile(r'(\d+|-\d+)\s*[=-]\s*(.+?)(?=(?:\s*;\s*\d+)|$)')
    matches = pattern.findall(notes)

    if not matches:
        return {
            "ui": {"inputType": "text"},
            "responseOptions": {"valueType": ["xsd:string"]}
        }

    choices = [{"name": {"en": option.strip()}, "value": int(value)} for value, option in matches]

    if len(choices) > 10:
        input_type = "select"
        multiple_choice = True
    else:
        input_type = "radio"
        multiple_choice = False

    response_options = {
        "valueType": ["xsd:integer"],
        "minValue": min(choice['value'] for choice in choices),
        "maxValue": max(choice['value'] for choice in choices),
        "multipleChoice": multiple_choice,
        "choices": choices
    }

    return {
        "ui": {"inputType": input_type},
        "responseOptions": response_options
    }

def detect_language(text):
    if not text or not isinstance(text, str) or text.strip() == '':
        logging.warning(f"Invalid or empty text for language detection: {text}")
        return None
    try:
        language = detect(text)
        logging.debug(f"Detected language: {language} for text: {text}")
        return language
    except LangDetectException as e:
        logging.warning(f"Language detection failed for text: {text}")
        return None

def split_var_label(var_label):
    pattern = re.compile(r'(?<=[?.])\s*')
    parts = pattern.split(var_label)
    split_parts = []
    for part in parts:
        split_parts.extend(part.split('/'))
    logging.debug(f"Split var_label into parts: {split_parts}")
    return [part.strip() for part in split_parts if part.strip()]

def create_js_expression(notes):
    notes = notes.replace('Calculation: ', '').strip()
    
    if notes.startswith('sum('):
        expression = notes[4:-1].replace('[', '').replace(']', '').replace(', ', ' + ')
    elif notes.startswith('mean('):
        variables = notes[5:-1].replace('[', '').replace(']', '').split(', ')
        expression = f"({ ' + '.join(variables) }) / {len(variables)}"
    elif 'if(' in notes and 'plus' in notes:
        expression = notes.replace('plus', '+').replace(' ', '')
    else:
        expression = notes.replace('[', '').replace(']', '').replace(', ', ' ').replace(' ', '')
    
    logging.debug(f"Created jsExpression: {expression}")
    return expression

def extract_special_cases(dataframe):
    special_cases = dataframe[dataframe["var_label"].str.contains("Subscale", na=False)]
    normal_cases = dataframe[~dataframe["var_label"].str.contains("Subscale", na=False)]
    logging.debug(f"Extracted special cases: {special_cases.shape}, normal cases: {normal_cases.shape}")
    return special_cases, normal_cases

def create_special_js_expression(var_label):
    if not isinstance(var_label, str):
        logging.error(f"Expected string for var_label, but got {type(var_label)}: {var_label}")
        return None, None, None
    
    pattern = re.compile(r'([A-Za-z\s\-]+), Mean: \((.+)\)/(\d+);?')
    match = pattern.match(var_label)
    if match:
        subscale_name = match.group(1).strip()
        js_expression = match.group(2).replace('[', '').replace(']', '').replace(', ', ' + ')
        num_items = match.group(3)
        js_expression = f"({js_expression}) / {num_items}"
        logging.debug(f"Created special jsExpression: {js_expression} for subscale: {subscale_name}")
        return subscale_name, js_expression, None
    
    pattern = re.compile(r'([A-Za-z\s\-]+), Mean: (.+)')
    match = pattern.match(var_label)
    if match:
        subscale_name = match.group(1).strip()
        description = match.group(2).strip()
        logging.debug(f"Found special case description: {description} for subscale: {subscale_name}")
        return subscale_name, None, description
    
    logging.debug(f"No special jsExpression found for var_label: {var_label}")
    return None, None, None

def create_special_item_schema(row, version):
    special_label, js_expression, description = create_special_js_expression(row['var_label'])
    
    if special_label and js_expression:
        item = {
            "category": "reproschema:Item",
            "id": row['var_name'],
            "prefLabel": {"en": f"{special_label}"},
            "description": {"en": f"{row['var_name']} of {row['table_name']}"},
            "schemaVersion": SCHEMA_VERSION,
            "version": version,
            "question": {"en": "Calculated value"},
            "ui": {"inputType": "number", "readonlyValue": True},
            "responseOptions": {"valueType": ["xsd:integer"]}
        }
        logging.debug(f"Created special item schema with jsExpression for {row['var_name']}")
        return item, js_expression
    
    elif special_label and description:
        item = {
            "category": "reproschema:Item",
            "id": row['var_name'],
            "prefLabel": {"en": special_label},
            "description": {"en": description},
            "schemaVersion": SCHEMA_VERSION,
            "version": version,
            "question": {"en": special_label},
            "ui": {"inputType": "number", "readonlyValue": True},
            "responseOptions": {"valueType": ["xsd:integer"]}
        }
        logging.debug(f"Created special item schema without jsExpression for {row['var_name']}")
        return item, None

    logging.debug(f"No special item schema created for {row['var_name']}")
    return None, None

def create_item_schema(row, version):
    def get_description():
        description = {"en": f"{row['var_name']} of {row['table_name']}"}
        if isinstance(row['notes'], str) and row['notes'].startswith("Note that"):
            description["en"] += f". {row['notes']}"
        return description

    def get_question(var_label):
        if not var_label or pd.isna(var_label):
            return {}

        en_label = ""
        es_label = ""
        parts = split_var_label(var_label)

        if parts:
            first_language = detect_language(parts[0])
            if first_language == 'es':
                es_label = var_label
            else:
                for part in parts:
                    language = detect_language(part)
                    if language == 'en':
                        en_label += part + ' '
                    elif language == 'es':
                        es_label += part + ' '
                    else:
                        if "¿" in part or "¡" in part or part.lower().startswith(("qué", "cuál", "cómo", "cuándo", "dónde")):
                            es_label += part + ' '
                        else:
                            en_label += part + ' '

        question = {}
        if en_label.strip():
            question["en"] = en_label.strip()
        if es_label.strip():
            question["es"] = es_label.strip()

        return question

    var_label = row['var_label'] if pd.notna(row['var_label']) else ''
    special_label, special_js_expression, description = create_special_js_expression(var_label)
    
    if special_label:
        item = {
            "category": "reproschema:Item",
            "id": row['var_name'],
            "prefLabel": {"en": special_label},
            "description": get_description(),
            "schemaVersion": SCHEMA_VERSION,
            "version": version,
            "question": {"en": "Calculated value"},
            "ui": {"inputType": "number", "readonlyValue": True},
            "responseOptions": {"valueType": ["xsd:integer"]}
        }
        logging.debug(f"Created item schema with special label for {row['var_name']}")
        return item, special_js_expression
    
    if pd.notna(row['notes']) and isinstance(row['notes'], str) and row['notes'].startswith("Calculation"):
        js_expression = create_js_expression(row['notes'])
        item = {
            "category": "reproschema:Item",
            "id": row['var_name'],
            "prefLabel": {"en": var_label},
            "description": get_description(),
            "schemaVersion": SCHEMA_VERSION,
            "version": version,
            "question": {"en": "Calculated value"},
            "ui": {"inputType": "number", "readonlyValue": True},
            "responseOptions": {"valueType": ["xsd:integer"]}
        }
        logging.debug(f"Created item schema with calculation notes for {row['var_name']}")
        return item, js_expression
    
    if row["type"] == "Date":
        ui = {"inputType": "date"}
        response_options = {"valueType": ["xsd:date"]}
    else:
        parsed_response = parse_notes(row['notes'])
        ui = parsed_response['ui']
        response_options = parsed_response['responseOptions']

    item = {
        "category": "reproschema:Item",
        "id": row['var_name'],
        "prefLabel": {"en": var_label},
        "description": get_description(),
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "question": get_question(var_label),
        "ui": ui,
        "responseOptions": response_options
    }
    logging.debug(f"Created item schema for {row['var_name']}")
    return item, None

def convert_condition_to_js(condition):
    if pd.isna(condition):
        return True  # No condition means it is always visible

    condition = condition.replace('==', '===')
    condition = condition.replace('AND', '&&').replace('OR', '||')
    condition = re.sub(r'\[([^\]]+)\]', r'\1', condition)
    
    logging.debug(f"Converted condition to js: {condition}")
    return condition

def create_activity_schema(activity_df, activity_label, activity_folder, version):
    activity_items = []
    compute_vars = []
    add_properties = []
    order = []

    special_cases, normal_cases = extract_special_cases(activity_df)
    
    for index, row in special_cases.iterrows():
        item_json, js_expression = create_special_item_schema(row, version)
        if item_json is not None:
            item_id = f"items/{row['var_name']}"
            
            if js_expression:
                compute_vars.append({
                    "variableName": row['var_name'],
                    "jsExpression": js_expression
                })
                add_properties.append({
                    "isAbout": item_id,
                    "variableName": row['var_name'],
                    "isVis": False
                })

                variables = extract_variables(js_expression)
                add_properties = update_add_properties(add_properties, variables)
            else:
                add_properties.append({
                    "isAbout": item_id,
                    "variableName": row['var_name'],
                    "isVis": False
                })
            
            it = Item(**item_json)
            file_path_item = os.path.join(activity_folder, "items", f'{row["var_name"]}')
            ensure_directory_exists(os.path.dirname(file_path_item))
            write_obj_jsonld(it, file_path_item, contextfile_url=SCHEMA_CONTEXT_URL)
            
            activity_items.append(item_json)
    
    for index, row in normal_cases.iterrows():
        item_json, js_expression = create_item_schema(row, version)
        if item_json is not None:
            item_id = f"items/{row['var_name']}"
            
            if js_expression:
                compute_vars.append({
                    "variableName": row['var_name'],
                    "jsExpression": js_expression
                })
                add_properties.append({
                    "isAbout": item_id,
                    "variableName": row['var_name'],
                    "isVis": False
                })

                variables = extract_variables(js_expression)
                add_properties = update_add_properties(add_properties, variables)
            else:
                condition_js = convert_condition_to_js(row['condition'])
                add_properties.append({
                    "isAbout": item_id,
                    "variableName": row['var_name'],
                    "valueRequired": True,
                    "isVis": condition_js
                })
                order.append(item_id)
            it = Item(**item_json)
            file_path_item = os.path.join(activity_folder, "items", f'{row["var_name"]}')
            ensure_directory_exists(os.path.dirname(file_path_item))
            write_obj_jsonld(it, file_path_item, contextfile_url=SCHEMA_CONTEXT_URL)
            
            activity_items.append(item_json)
    
    table_name = activity_label.replace(" ", "_")
    table_label = activity_label.title()
    sub_domain = activity_df['sub_domain'].iloc[0]
    domain = activity_df['domain'].iloc[0]
    
    activity_schema = {
        "category": "reproschema:Activity",
        "id": f"{table_name}_schema",
        "prefLabel": {"en": table_label},
        "description": {"en": f"This activity is about {sub_domain} in {domain}"},
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "compute": compute_vars,
        "ui": {
            "addProperties": add_properties,
            "order": order,
            "shuffle": False
        }
    }

    act = Activity(**activity_schema)

    path = os.path.join(activity_folder)
    ensure_directory_exists(path)
    filename = f"{table_name}_schema"
    file_path = os.path.join(path, filename)
    write_obj_jsonld(act, file_path, contextfile_url=SCHEMA_CONTEXT_URL)
    logging.info(f"{table_name} Instrument schema created")

def create_protocol_schema(
    protocol_folder,
    version,
    protocol_name,
    protocol_display_name,
    protocol_description,
    protocol_order,
    protocol_visibility_obj,
):
    protocol_schema = {
        "category": "reproschema:Protocol",
        "id": f"{protocol_name}_schema",
        "prefLabel": {"en": protocol_display_name},
        "altLabel": {"en": f"{protocol_name}_schema"},
        "description": {"en": protocol_description},
        "schemaVersion": SCHEMA_VERSION,
        "version": version,
        "ui": {
            "addProperties": [],
            "order": [],
            "shuffle": False,
        },
    }

    for activity in protocol_order:
        full_path = f"../activities/{activity}/{activity}_schema"
        add_property = {
            "isAbout": full_path,
            "variableName": f"{activity}_schema",
            "isVis": protocol_visibility_obj.get(activity, True),
        }
        protocol_schema["ui"]["addProperties"].append(add_property)
        protocol_schema["ui"]["order"].append(full_path)

    prot = Protocol(**protocol_schema)
    schema_file = f"{protocol_name}_schema"
    file_path = os.path.join(protocol_folder, schema_file)
    write_obj_jsonld(prot, file_path, contextfile_url=SCHEMA_CONTEXT_URL)
    logging.info(f"Protocol schema created in {file_path}")

def convert_csv_to_reproschema(csv_file, version):
    csv_file_path = os.path.abspath(csv_file)
    output_folder = os.path.dirname(csv_file_path)

    dataframe = read_csv(csv_file)
    dataframe = filter_domains(dataframe, "Imaging")
    protocol_dfs = get_protocol_dfs(dataframe)
    
    for protocol_df in protocol_dfs:
        protocol_id = protocol_df['study'].iloc[0].lower()
        protocol_description =f"This protocol is about {protocol_id} in the Adolescent Brain Cognitive Development (ABCD) Study"
        protocol_folder = os.path.join(output_folder, protocol_id)
        ensure_directory_exists(protocol_folder)
        
        activities = protocol_df['table_name'].unique().tolist()
        protocol_order = [activity.replace(" ", "_") for activity in activities]
        
        for activity in activities:
            activity_df = protocol_df[protocol_df['table_name'] == activity]
            activity_folder = os.path.join(protocol_folder, "activities", activity.replace(" ", "_"))
            ensure_directory_exists(activity_folder)
            
            create_activity_schema(activity_df, activity, activity_folder, version)
        
        protocol_visibility_obj = {activity.replace(" ", "_"): True for activity in activities}
        create_protocol_schema(
            protocol_folder,
            version,
            protocol_id,
            protocol_id.replace("_", " ").title(),
            protocol_description,
            protocol_order,
            protocol_visibility_obj
        )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Convert CSV to Reproschema format.")
    parser.add_argument("--file_path", type=str, required=True, help="Path to the input CSV file.")
    parser.add_argument("--version", type=str, required=True, help="Schema version.")

    args = parser.parse_args()

    logging.debug(f"Starting conversion with file_path: {args.file_path} and version: {args.version}")
    convert_csv_to_reproschema(args.file_path, args.version)