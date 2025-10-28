import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from notion_client import Client
from datetime import timedelta

# --- 1. 설정 및 초기화 ---
# Streamlit Secrets에서 API 토큰과 DB ID를 안전하게 가져옵니다.
notion_token = st.secrets["NOTION_TOKEN"]
db_id = st.secrets["DATABASE_ID"]

# Streamlit 앱 페이지의 기본 설정을 지정합니다.
st.set_page_config(layout="wide", page_title="프로젝트 마일스톤 타임라인")

# Notion API 클라이언트 인스턴스를 인증 토큰으로 초기화합니다.
notion = Client(auth=notion_token)

# Plotly 설정 (경고 제거 및 기본 설정)
plotly_config = {
    'displaylogo': False,
    'displayModeBar': True,
    'responsive': True
}

# --- 2. Notion 데이터 가져오기 (캐시 적용) ---
@st.cache_data(ttl=600) # 10분마다 데이터를 새로고침
def get_notion_database_data(database_id: str) -> list:
    """지정된 Notion 데이터베이스에서 모든 페이지(항목) 데이터를 가져옵니다."""
    all_results = []
    start_cursor = None

    while True:
        try:
            response = notion.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                sorts=[
                    {"property": "이름", "direction": "ascending"}
                ]
            )
            all_results.extend(response["results"])
            if not response["has_more"]:
                break
            start_cursor = response["next_cursor"]
        except Exception as e:
            st.error(f"Notion 데이터 로드 중 오류가 발생했습니다: {e}")
            return []
    return all_results

# --- 3. Project DB 이름 조회 함수 ---
def get_page_title_by_id(page_id: str) -> str:
    """페이지 ID를 사용하여 해당 페이지의 제목을 조회합니다. 제목 속성(Title type)을 찾아 반환합니다."""
    try:
        page = notion.pages.retrieve(page_id=page_id)
        for prop_name, prop_data in page["properties"].items():
            if prop_data.get("type") == "title":
                title_prop = prop_data.get("title", [])
                return title_prop[0]["plain_text"] if title_prop else "이름 없음"
        return "이름 없음"
    except Exception:
        return "이름 없음"

# --- 4. Notion 데이터 가공 (예외 처리 강화) ---
@st.cache_data(ttl=600)
def process_notion_data(notion_pages: list) -> pd.DataFrame:
    """
    가져온 Notion 페이지 데이터를 Pandas DataFrame으로 가공합니다.
    - 모든 속성 추출 시 비어있거나 타입이 맞지 않아도 안전하게 처리합니다.
    """
    processed_items = []
    for item in notion_pages:
        properties = item.get("properties", {})

        # 1. '이름' 속성 추출 (Title 타입)
        name_prop = properties.get("이름", {}).get("title", [])
        project_name = name_prop[0]["plain_text"] if name_prop else "이름 없음" # None 대신 문자열 기본값 사용

        # '상위 항목' 관계 속성 추출
        parent_relation_prop = properties.get("상위 항목", {}).get("relation", [])
        parent_id = parent_relation_prop[0]["id"] if parent_relation_prop else None
        
        # 최상위 항목 이름 대체 로직
        if parent_id is None:
            project_db_relation = properties.get("🏠 Project DB", {}).get("relation", [])
            if project_db_relation:
                project_db_id = project_db_relation[0]["id"]
                project_name = get_page_title_by_id(project_db_id) 

        if project_name == "이름 없음": continue # 이름이 없으면 스킵

        # 2. '구분' 속성 추출 및 안전 처리 (Select 타입)
        item_type = "미분류"
        type_prop = properties.get("구분") 
        if type_prop and type_prop.get("type") == "select":
            item_type = type_prop.get("select", {}).get("name") if type_prop.get("select") else "미분류"
        
        # 3. '타임라인' 속성 추출 (Date 타입)
        # 타임라인이 비어있으면 None으로 처리되어, Pandas 변환 시 NaT로 처리됩니다. (안전)
        end_date_obj = properties.get("타임라인", {}).get("date")
        end_date = end_date_obj["start"] if end_date_obj and "start" in end_date_obj else None
        
        # 4. '상태' 속성 추출 (Status 또는 Select 타입)
        status_prop = properties.get("진행 상태", {})
        status = "미정"
        if status_prop.get("type") == "status" and status_prop.get("status"):
            status = status_prop["status"].get("name", "미정")
        elif status_prop.get("type") == "select" and status_prop.get("select"):
            status = status_prop["select"].get("name", "미정")

        processed_items.append({
            "id": item["id"],
            "이름": project_name,
            "타임라인": end_date,
            "상태": status,
            "구분": item_type,
            "상위 항목 ID": parent_id,
        })
    
    df = pd.DataFrame(processed_items)
    
    # Critical: '타임라인' 컬럼이 존재하도록 보장 후 변환
    if '타임라인' in df.columns:
        df["타임라인"] = pd.to_datetime(df["타임라인"], errors='coerce')
    else:
        df['타임라인'] = pd.NaT 
    
    df['구분_lower'] = df['구분'].str.lower()
    
    return df

# --- 5. 하위 태스크 데이터 수집 ---
def get_descendant_end_details(task_id: str, df_all_tasks_indexed: pd.DataFrame, parent_child_map: dict) -> list:
    descendant_details = []
    
    if task_id in parent_child_map:
        for child_id in parent_child_map.get(task_id, []):
            try:
                child_task = df_all_tasks_indexed.loc[[child_id]]
            except KeyError:
                child_task = pd.DataFrame()

            # 유효성 검사 (타임라인, 이름, 상태가 유효할 때만 추가)
            if (not child_task.empty and 
                pd.notna(child_task["타임라인"].iloc[0]) and
                child_task["이름"].iloc[0] != "이름 없음" and
                child_task["상태"].iloc[0] != "미정" ):
                descendant_details.append({
                    'date': child_task["타임라인"].iloc[0],
                    'name': child_task["이름"].iloc[0],
                    'status': child_task["상태"].iloc[0]
                })
            descendant_details.extend(get_descendant_end_details(child_id, df_all_tasks_indexed, parent_child_map))
            
    return descendant_details

# --- 6. 타임라인 차트 생성 ---
def create_timeline_chart(df_filtered: pd.DataFrame, df_full_data: pd.DataFrame) -> go.Figure:
    """
    필터링된 데이터를 사용하여 타임라인 차트를 생성하고, 최상위 항목의 '구분'에 따라 색상을 적용합니다.
    """
    # Y축 라벨로 사용할 최상위 항목 (df_filtered에서 부모가 없는 항목)
    top_level_tasks = df_filtered[df_filtered["상위 항목 ID"].isnull()].copy()
    
    # --- 정렬 순서 수정: project > project/poc hybrid > poc 순 ---
    def get_sort_key(item_type):
        if item_type == 'project':
            return 0  # project만 있는 항목 (최우선)
        elif item_type == 'poc':
            return 2  # poc만 있는 항목 (최후순)
        else:
            return 1  # 그 외 항목 (중간)
    
    top_level_tasks['sort_key'] = top_level_tasks['구분_lower'].apply(get_sort_key)
    top_level_tasks = top_level_tasks.sort_values(
        by=['sort_key', '이름'], 
        ascending=[True, True]
    ).drop(columns=['sort_key']) 

    fig = go.Figure()

    # 재귀 탐색을 위한 부모 -> 자식 ID 맵을 전체 데이터에서 생성
    parent_child_map = {}
    for _, row in df_full_data.iterrows():
        if pd.notna(row["상위 항목 ID"]) and row["상위 항목 ID"] in top_level_tasks['id'].tolist():
            parent_child_map.setdefault(row["상위 항목 ID"], []).append(row["id"])

    # X축 범위 계산 및 데이터 준비
    all_descendant_end_dates = []
    df_indexed_by_id = df_full_data.set_index('id') 

    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        descendant_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        all_descendant_end_dates.extend([d['date'] for d in descendant_details])

    valid_end_dates = pd.Series(all_descendant_end_dates).dropna()
    min_date = valid_end_dates.min() if not valid_end_dates.empty else pd.Timestamp.now() - timedelta(days=30)
    max_date = valid_end_dates.max() if not valid_end_dates.empty else pd.Timestamp.now() + timedelta(days=30)
    
    # --- Y축 간격 확보를 위한 숫자 매핑 ---
    y_axis_spacing_factor = 60.0 
    y_axis_map = {name: i * y_axis_spacing_factor for i, name in enumerate(top_level_tasks["이름"].tolist())}
    y_tickvals = list(y_axis_map.values())
    y_ticktext = list(y_axis_map.keys())
    y_range_min = y_tickvals[-1] + y_axis_spacing_factor * 0.5 if y_tickvals else 1.0
    y_range_max = y_tickvals[0] - y_axis_spacing_factor * 0.5 if y_tickvals else 0.0
    
    # Y축 카테고리 설정용 더미 Scatter 트레이스 추가
    fig.add_trace(go.Scatter(
        x=[min_date, max_date], 
        y=[y_axis_map.get(name, 0) for name in top_level_tasks["이름"].tolist()],
        mode='text', showlegend=False, hoverinfo='skip'
    ))

    fig.update_yaxes(autorange="reversed") 

    plotly_qualitative_colors = px.colors.qualitative.Plotly 
    
    # 하위 항목 이름에 색상을 매핑하는 딕셔너리를 생성합니다.
    all_descendant_names = sorted(list(set(df_full_data[df_full_data['상위 항목 ID'].notnull()]['이름'].unique())))
    color_map = {}
    for i, name in enumerate(all_descendant_names):
        color_map[name] = plotly_qualitative_colors[i % len(plotly_qualitative_colors)]
    
    # --- 색상 조건 설정: project는 흰색, poc는 진한 파란색 (다크 테마용) ---
    color_map_main = {
        'project': 'white', 
        'poc': 'dodgerblue', 
        '미분류': 'gray'
    }
    
    # Y축 라벨 폰트 색상을 저장할 딕셔너리
    label_color_map = {} 

    # 각 최상위 태스크에 대한 타임라인 트레이스를 추가합니다.
    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        top_task_name = top_task["이름"]
        top_task_type = top_task["구분_lower"] 

        line_dot_color = color_map_main.get(top_task_type, 'gray')
        
        label_color_map[top_task_name] = line_dot_color
        
        descendant_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        
        if descendant_details:
            descendant_details.sort(key=lambda x: x['date'])
            
            x_coords = [d['date'] for d in descendant_details]
            y_coords = [y_axis_map[top_task_name]] * len(x_coords)
            
            # 호버 텍스트 구성
            hover_texts = [
                f"<b>프로젝트: {top_task_name}</b><br>"
                f"<b>태스크: {d['name']}</b><br>"
                f"날짜: {d['date'].strftime('%Y/%m/%d')}"
                for d in descendant_details
            ]

            # 하위 항목 이름에 따라 점 색상 할당
            colors_for_points = [color_map.get(d['name'], 'lightgray') for d in descendant_details]
            
            # Scatter 트레이스 추가
            fig.add_trace(
                go.Scatter(
                    x=x_coords,
                    y=y_coords,
                    mode='lines+markers',
                    marker=dict(
                        symbol='circle',
                        size=15,
                        color=colors_for_points, 
                        line=dict(width=1, color=line_dot_color) 
                    ),
                    line=dict(color=line_dot_color, width=3), 
                    name=f"{top_task_name} 하위 타임라인",
                    hoverinfo='text',
                    hovertext=hover_texts,
                    showlegend=False
                )
            )
    
    # Y축 틱 텍스트에 색상 적용 (가장 좌측 타이틀)
    colored_y_ticktext = [
        f'<span style="color:{label_color_map.get(text, "gray")};">{text}</span>'
        for text in y_ticktext
    ]

    # 차트의 전체 레이아웃을 설정합니다.
    fig.update_layout(
        template='plotly_dark', # 전체 차트 배경을 다크 테마로 고정
        title="", 
        # X축 제목 및 폰트 설정
        xaxis_title=dict(
            text="날짜",
            font=dict(size=20)
        ),
        # Y축 제목 및 폰트 설정
        yaxis_title=dict(
            text="프로젝트",
            font=dict(size=20)
        ),
        hovermode="closest",
        xaxis=dict(
            autorange=True,
            showgrid=True,
            tickformat="%Y/%m/%d",
            tickfont=dict(size=14)
        ), 
        # Y축 라벨 폰트 크기 조정 및 자동 마진 설정
        yaxis=dict(
            showgrid=True,
            tickfont=dict(size=16), 
            automargin=True,
            ticklen=5,
            type='linear', 
            tickmode='array', 
            tickvals=y_tickvals, 
            ticktext=colored_y_ticktext, 
            range=[y_range_min, y_range_max],
            fixedrange=False,
        ),
        margin=dict(l=150, r=20, t=20, b=20), # 좌측 마진(l) 대폭 증가
    )

    return fig, top_level_tasks

# --- 7. Streamlit 앱 실행 로직 ---
if __name__ == "__main__":
    if not notion_token or not db_id:
        st.error("Streamlit Secrets(`NOTION_TOKEN`, `DATABASE_ID`)이 설정되지 않았습니다.")
        st.info("Secrets를 설정해주세요.")
    else:
        # 1. 데이터 로드 (캐시 적용)
        df_full_data = process_notion_data(get_notion_database_data(db_id))

        if not df_full_data.empty:
            # '구분' 컬럼의 고유 값 추출 및 소문자 변환
            unique_types = df_full_data['구분'].str.lower().unique()
            all_types = sorted([t for t in unique_types if t not in ['미분류', None, '']])
            
            # 기본값 설정
            default_selection = []
            if 'project' in all_types:
                default_selection.append('project')
            if 'poc' in all_types:
                default_selection.append('poc')

            # 사이드바에 필터 버튼 추가
            with st.sidebar:
                st.header("프로젝트 필터")
                selected_types = st.multiselect(
                    "구분 값 선택",
                    options=all_types,
                    default=default_selection
                )
            
            # 2. 데이터 필터링
            if selected_types:
                # 선택된 값으로 필터링할 최상위 항목 ID 목록을 찾습니다.
                main_project_ids = df_full_data[
                    (df_full_data['구분_lower'].isin(selected_types)) & 
                    (df_full_data['상위 항목 ID'].isnull())
                ]['id'].tolist()

                df_filtered = df_full_data[df_full_data['id'].isin(main_project_ids)].copy()
                
            else:
                df_filtered = pd.DataFrame() 

            # 3. 차트 표시
            if not df_filtered.empty:
                # Streamlit 컴포넌트 제목 표시 (좌측 정렬 및 폰트 크기 20px)
                st.markdown(
                    """
                    <div style="background-color:#FFA500; color:white; padding:10px; border-radius:5px; text-align:left; font-size:20px; margin-bottom: 20px;">
                        <b>프로젝트 일정 Summary</b>
                    </div>
                    """,
                    unsafe_allow_html=True 
                )
                
                # 필터링된 데이터를 사용하여 차트 생성
                chart_figure, top_level_tasks_plot = create_timeline_chart(df_filtered, df_full_data) 
                
                # Y축 높이 동적 계산
                num_categories = len(top_level_tasks_plot)
                height_per_category = 80
                min_chart_height = 250
                dynamic_height = max(min_chart_height, num_categories * height_per_category)

                # Plotly config 적용
                st.plotly_chart(chart_figure, use_container_width=True, height=dynamic_height, config=plotly_config)
            else:
                st.warning("선택된 필터 조건에 해당하는 프로젝트가 없습니다.")
        else:
            st.info("Notion 데이터베이스에서 데이터를 가져오지 못했습니다. API 설정 또는 네트워크 연결을 확인해주세요.")