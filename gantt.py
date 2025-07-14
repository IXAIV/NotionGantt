import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from notion_client import Client
from datetime import timedelta

# --- 1. 설정 및 초기화 ---
# Notion API 인증 토큰과 데이터베이스 ID를 설정합니다.
notion_token = st.secrets["NOTION_TOKEN"]
db_id = st.secrets["DATABASE_ID"]

# Streamlit 앱 페이지의 기본 레이아웃과 제목을 설정합니다.
st.set_page_config(layout="wide", page_title="프로젝트 마일스톤 타임라인")

# Notion API 클라이언트 인스턴스를 인증 토큰으로 초기화합니다.
notion = Client(auth=notion_token)

# --- 2. Notion 데이터 가져오기 ---
# @st.cache_data(ttl=600) # 데이터를 10분마다 새로고침하여 Notion API 호출 횟수를 최적화합니다. (현재 주석 처리됨)
def get_notion_database_data(database_id: str) -> list:
    """
    지정된 Notion 데이터베이스에서 모든 페이지(항목) 데이터를 가져옵니다.
    데이터는 '이름' 속성을 기준으로 오름차순으로 정렬됩니다.
    API 호출 중 오류가 발생하면 오류 메시지를 표시하고 빈 리스트를 반환합니다.
    """
    all_results = []
    start_cursor = None # 페이지네이션을 위한 커서 변수 초기화

    while True:
        try:
            # Notion 데이터베이스 쿼리를 실행하여 데이터를 가져옵니다.
            response = notion.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                sorts=[
                    {"property": "이름", "direction": "ascending"} # '이름'으로 오름차순 정렬
                ]
            )
            all_results.extend(response["results"]) # 가져온 결과를 전체 리스트에 추가합니다.
            
            # Notion에 더 이상 가져올 데이터가 없으면 반복을 중단합니다.
            if not response["has_more"]:
                break
            start_cursor = response["next_cursor"] # 다음 페이지를 가져오기 위해 커서를 업데이트합니다.
        except Exception as e:
            # Notion 데이터 로드 중 예외 발생 시 오류 메시지를 표시합니다.
            st.error(f"Notion 데이터 로드 중 오류가 발생했습니다: {e}")
            return [] # 오류 발생 시 빈 리스트를 반환하여 처리합니다.
    return all_results # 모든 데이터를 반환합니다.

def get_page_title_by_id(page_id: str) -> str:
    try:
        page = notion.pages.retrieve(page_id=page_id)
        title_prop = page["properties"].get("Project", {}).get("title",[])
        if title_prop:
            return title_prop[0]["plain_text"]
        else:
            return "이름 없음"
    except Exception as e:
        st.warning(f"project DB 이름 조회 실패: {e}")
        return "이름 없음"

# --- 3. Notion 데이터 가공 ---
def process_notion_data(notion_pages: list) -> pd.DataFrame:
    """
    가져온 Notion 페이지 데이터를 분석 및 시각화에 적합한 Pandas DataFrame으로 가공합니다.
    - 각 항목에서 '이름', '타임라인', '상태', '상위 항목 ID'를 추출합니다.
    - '이름'이 "이름 없음"인 항목은 데이터 처리에서 제외합니다.
    - '타임라인'은 날짜/시간(datetime) 객체로 변환하며, 유효하지 않은 날짜는 NaT(Not a Time)로 처리합니다.
    """
    processed_items = []
    for item in notion_pages:
        properties = item.get("properties", {}) # Notion 페이지의 속성 정보를 가져옵니다.

        # '이름' 속성 (Title 타입)을 추출합니다.
        name_prop = properties.get("이름", {}).get("title", [])
        project_name = name_prop[0]["plain_text"] if name_prop else "이름 없음"

        # '상위 항목' 관계 속성 (Relation 타입)을 추출하여 부모 ID를 가져옵니다.
        parent_relation_prop = properties.get("상위 항목", {}).get("relation", [])
        parent_id = parent_relation_prop[0]["id"] if parent_relation_prop else None

        # 최상위 항목이면 project DB 이름으로 대체
        if parent_id is None:
            project_db_relation = properties.get("🏠 Project DB", {}).get("relation", [])
            if project_db_relation:
                project_db_id = project_db_relation[0]["id"]
                project_name = get_page_title_by_id(project_db_id)

        # '이름 없음'으로 지정된 항목은 타임라인 시각화에서 제외합니다.
        if project_name == "이름 없음":
            continue

        # '타임라인' 속성 (Date 타입)의 'start' 필드를 추출합니다.
        end_date_obj = properties.get("타임라인", {}).get("date")
        end_date = end_date_obj["start"] if end_date_obj and "start" in end_date_obj else None
        
        # '상태' 속성 (Status 또는 Select 타입)을 추출합니다.
        status_prop = properties.get("진행 상태", {})
        status = status_prop.get("status", {}).get("name") if status_prop.get("type") == "status" else \
                 status_prop.get("select", {}).get("name") if status_prop.get("type") == "select" else "미정"

        # 가공된 데이터를 리스트에 추가합니다.
        processed_items.append({
            "id": item["id"],
            "이름": project_name,
            "타임라인": end_date,
            "상태": status,
            "상위 항목 ID": parent_id,
        })
    
    # 가공된 아이템 리스트를 Pandas DataFrame으로 변환합니다.
    df = pd.DataFrame(processed_items)
    
    # '타임라인' 컬럼을 datetime 형식으로 변환합니다. 변환 중 오류가 발생하면 해당 값을 NaT로 처리합니다.
    df["타임라인"] = pd.to_datetime(df["타임라인"], errors='coerce')
    
    return df # 가공된 DataFrame을 반환합니다.

# --- 4. 하위 태스크 데이터 수집 ---
def get_descendant_end_details(task_id: str, df_all_tasks_indexed: pd.DataFrame, parent_child_map: dict) -> list:
    """
    주어진 `task_id`에 해당하는 상위 태스크의 모든 하위(자식, 손자 등) 태스크의 타임라인과 세부 정보를 재귀적으로 수집합니다.
    이 함수는 최상위 태스크 자체의 타임라인은 포함하지 않고, 오직 그 하위 태스크만 탐색합니다.
    `df_all_tasks_indexed`는 'id'를 인덱스로 설정한 DataFrame으로, 효율적인 데이터 조회를 위해 사용됩니다.
    """
    descendant_details = []
    
    # 현재 태스크 ID가 부모-자식 맵에 존재하면, 해당 태스크의 자식들을 탐색합니다.
    if task_id in parent_child_map:
        for child_id in parent_child_map.get(task_id, []):
            try:
                # 자식 태스크의 정보를 'id' 인덱스를 통해 DataFrame에서 직접 조회합니다.
                child_task = df_all_tasks_indexed.loc[[child_id]]
            except KeyError:
                # 만약 자식 ID가 DataFrame에 없으면 (예: "이름 없음"으로 필터링된 경우) 빈 DataFrame으로 처리합니다.
                child_task = pd.DataFrame() 

            # 자식 태스크 데이터가 존재하고 '타임라인'이 유효한 경우, 세부 정보를 추가합니다.
            if not child_task.empty and pd.notna(child_task["타임라인"].iloc[0]):
                descendant_details.append({
                    'date': child_task["타임라인"].iloc[0],
                    'name': child_task["이름"].iloc[0],
                    'status': child_task["상태"].iloc[0]
                })
            # 현재 자식 태스크를 기준으로 재귀 호출하여 그 하위 태스크들을 계속 탐색합니다.
            descendant_details.extend(get_descendant_end_details(child_id, df_all_tasks_indexed, parent_child_map))
            
    return descendant_details # 수집된 모든 하위 태스크 상세 정보를 반환합니다.

# --- 5. 타임라인 차트 생성 ---
def create_timeline_chart(df: pd.DataFrame) -> go.Figure:
    """
    가공된 Pandas DataFrame을 사용하여 Plotly의 점 연결 타임라인 차트(Gantt 차트와 유사)를 생성합니다.
    - **Y축**: Notion 데이터베이스에서 '상위 항목' 관계가 없는 **최상위 프로젝트의 이름**을 표시합니다.
    - **점과 선**: 각 최상위 프로젝트에 속한 **하위 태스크들의 타임라인**이 점으로 표시되며, 이 점들은 선으로 연결됩니다.
    - **X축(날짜)**: 모든 하위 태스크의 타임라인을 기반으로 자동으로 범위가 설정됩니다.
    - **가독성**: X축 및 Y축의 제목과 라벨 폰트 크기를 조정하여 가독성을 높입니다.
    - **색상**: 동일한 하위 항목 이름에는 같은 색상이 적용되어 시각적으로 구분하기 쉽습니다.
    - **상시 표시 텍스트**: 각 점 옆에는 해당 하위 항목의 이름과 날짜가 항상 표시됩니다.
    - **호버 정보**: 마우스 커서를 점에 올리면(호버) 상위 이름, 하위 태스크 이름, 그리고 정확한 날짜가 상세하게 표시됩니다.
    """
    # '상위 항목 ID'가 없는 항목들을 최상위 프로젝트로 간주하고 복사본을 생성합니다.
    top_level_tasks = df[df["상위 항목 ID"].isnull()].copy()
    
    # Y축 라벨의 순서를 위해 최상위 프로젝트들을 이름 기준으로 오름차순 정렬합니다.
    top_level_tasks = top_level_tasks.sort_values(by="이름", ascending=True) 

    # Plotly Figure 객체를 초기화하여 차트를 그리기 시작합니다.
    fig = go.Figure()

    # 재귀 탐색을 위해 '부모 ID -> 자식 ID 리스트' 형태의 맵을 생성합니다.
    parent_child_map = {}
    for _, row in df.iterrows():
        if pd.notna(row["상위 항목 ID"]) and row["상위 항목 ID"] in df['id'].values:
            parent_child_map.setdefault(row["상위 항목 ID"], []).append(row["id"])

    all_descendant_end_dates = [] # 모든 하위 태스크의 타임라인을 저장할 리스트
    df_indexed_by_id = df.set_index('id') # 'id'를 인덱스로 설정하여 데이터 조회 성능을 높입니다.

    # 모든 최상위 프로젝트에 대해 하위 태스크의 타임라인을 수집합니다.
    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        descendant_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        all_descendant_end_dates.extend([d['date'] for d in descendant_details])

    # 유효한(NaT가 아닌) 타임라인만 필터링하여 X축 범위 계산에 사용합니다.
    valid_end_dates = pd.Series(all_descendant_end_dates).dropna()

    # X축(날짜) 범위의 최소값과 최대값을 설정합니다.
    # 유효한 타임라인이 없는 경우, 현재 날짜를 기준으로 기본 범위를 설정합니다.
    min_date = valid_end_dates.min() if not valid_end_dates.empty else pd.Timestamp.now() - timedelta(days=30)
    max_date = valid_end_dates.max() if not valid_end_dates.empty else pd.Timestamp.now() + timedelta(days=30)
    
    # Plotly의 기본 색상 팔레트를 가져와 하위 항목 색상 매핑에 사용합니다.
    plotly_qualitative_colors = px.colors.qualitative.Plotly 
    
    # 모든 고유한 하위 항목 이름을 추출하여, 각 이름에 고유한 색상을 할당할 준비를 합니다.
    all_descendant_names = sorted(list(set(d['name'] for top_task in top_level_tasks.iterrows() 
                                            for d in get_descendant_end_details(top_task[1]["id"], df_indexed_by_id, parent_child_map))))

    # 하위 항목 이름에 색상을 매핑하는 딕셔너리를 생성합니다.
    color_map = {}
    for i, name in enumerate(all_descendant_names):
        color_map[name] = plotly_qualitative_colors[i % len(plotly_qualitative_colors)]

    # --- Y축 라벨 간격 및 위치 제어 설정 ---
    # 각 이름에 고유한 Y축 숫자 값을 매핑합니다.
    # 이 숫자 값의 간격(`y_axis_spacing_factor`)이 Y축 라벨의 시각적 간격을 결정합니다.
    # `y_axis_spacing_factor`를 조절하여 Y축 라벨 사이의 세로 간격을 조정할 수 있습니다.
    y_axis_spacing_factor = 20.0 # 간격 계수: 1.0이 기본 간격, 값이 커질수록 라벨 간격이 넓어집니다.

    # Y축 라벨(이름)에 대한 숫자 매핑 딕셔너리를 생성합니다.
    y_axis_map = {name: i * y_axis_spacing_factor for i, name in enumerate(top_level_tasks["이름"].tolist())} 

    # Plotly Y축에 실제로 표시될 숫자 값(`y_tickvals`)과 그에 대응하는 텍스트 라벨(`y_ticktext`)을 생성합니다.
    y_tickvals = list(y_axis_map.values())
    y_ticktext = list(y_axis_map.keys())

    # Y축의 표시 범위를 설정합니다. 가장 낮은 값부터 가장 높은 값까지, 그리고 시각적 여백을 추가합니다.
    # Y축 순서를 뒤집어 최신 프로젝트가 상단에 오도록 범위를 설정합니다.
    y_range_min = y_tickvals[-1] + 1 * y_axis_spacing_factor if y_tickvals else 0 # 가장 큰 Y값에 여백 추가
    y_range_max = y_tickvals[0] - 1 * y_axis_spacing_factor if y_tickvals else 0 # 가장 작은 Y값에 여백 추가 (음수일 수 있음)
    
    # 프로젝트가 하나도 없는 경우를 대비하여 기본 Y축 범위를 설정합니다.
    if not y_tickvals:
        y_range_min = 1.0 
        y_range_max = 0.0

    # -----------------------------------------------------------

    # 각 최상위 프로젝트에 대한 타임라인 트레이스(선과 점)를 추가합니다.
    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        top_task_name = top_task["이름"] # 현재 최상위 프로젝트의 이름

        # 현재 최상위 프로젝트에 속한 모든 하위 태스크의 상세 타임라인 정보를 가져옵니다.
        descendant_end_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        
        if descendant_end_details: # 하위 태스크가 존재하는 경우에만 차트에 추가합니다.
            # 점들을 날짜 순으로 정렬하여 선이 올바르게 연결되도록 합니다.
            descendant_end_details.sort(key=lambda x: x['date'])
            
            x_coords = [d['date'] for d in descendant_end_details] # X축(날짜) 좌표
            y_coords = [y_axis_map[top_task_name]] * len(x_coords) # Y축(프로젝트) 좌표 (숫자 매핑 사용)
            
            # 각 점 옆에 상시 표시될 텍스트를 구성합니다 (태스크 이름과 월/일).
            point_texts = [
                f"{d['name']} ({d['date'].strftime('%m/%d')})" 
                for d in descendant_end_details
            ]

            # 마우스 호버 시 표시될 상세 텍스트를 구성합니다 (프로젝트, 태스크, 전체 날짜).
            hover_texts = [
                f"<b>프로젝트: {top_task_name}</b><br>"
                f"<b>태스크: {d['name']}</b><br>"
                f"날짜: {d['date'].strftime('%Y/%m/%d')}"
                for d in descendant_end_details
            ]
            
            # 각 점에 적용할 색상을 하위 항목 이름에 따라 결정합니다.
            colors_for_points = [color_map[d['name']] for d in descendant_end_details]
            
            # Plotly Scatter 트레이스를 추가하여 점, 선, 텍스트를 그립니다.
            fig.add_trace(
                go.Scatter(
                    x=x_coords,
                    y=y_coords, # Y축에 매핑된 숫자 값을 사용
                    mode='lines+markers+text', # 선, 마커(점), 텍스트를 모두 표시
                    marker=dict(
                        symbol='circle', # 원형 마커 사용
                        size=15,         # 마커 크기
                        color=colors_for_points, # 하위 항목별 색상 적용
                        line=dict(width=1, color='DarkSlateGrey') # 마커 테두리 색상
                    ),
                    line=dict(color='DarkSlateGrey', width=5), # 선의 색상과 두께 설정
                    name=f"{top_task_name} 하위 타임라인", # 이 트레이스의 이름 (범례에 표시 안됨)
                    text=point_texts, # 각 점 옆에 표시될 텍스트
                    textposition='bottom center', # 텍스트 위치를 점 아래 중앙으로 설정
                    hoverinfo='text', # 호버 시 'hovertext' 속성만 표시
                    hovertext=hover_texts, # 마우스 호버 시 나타날 상세 텍스트
                    showlegend=False # 이 트레이스를 범례에 표시하지 않음
                )
            )
    
    # 차트의 전체 레이아웃을 설정합니다.
    fig.update_layout(
        title="", # 차트 제목 (현재 비어 있음)
        xaxis_title=dict(
            text="날짜", # X축 제목
            font=dict(size=20) # X축 제목 폰트 크기
        ),
        yaxis_title=dict(
            text="프로젝트", # Y축 제목
            font=dict(size=20) # Y축 제목 폰트 크기
        ),
        xaxis=dict(
            autorange=True, # X축 범위를 데이터에 맞춰 자동으로 설정
            showgrid=True, # X축 그리드 라인 표시
            tickformat="%Y/%m/%d", # X축 틱 라벨 날짜 형식
            tickfont=dict(size=14) # X축 틱 라벨 폰트 크기
        ), 
        yaxis=dict(
            showgrid=True, # Y축 그리드 라인 표시
            tickfont=dict(size=16), # Y축 틱 라벨 폰트 크기
            automargin=True, # Y축 라벨이 잘리지 않도록 자동 마진 설정
            ticklen=5, # Y축 틱 마크 길이
            type='linear', # Y축 타입을 'linear' (선형)으로 설정 (숫자 값 사용에 적합)
            tickmode='array', # 틱 값과 텍스트를 배열로 제공하여 수동 설정
            tickvals=y_tickvals, # 수동으로 생성한 틱 값 (숫자)
            ticktext=y_ticktext, # 수동으로 생성한 틱 텍스트 (이름)
            range=[y_range_min, y_range_max], # Y축 범위 설정 (최소값, 최대값)
            fixedrange=True, # 사용자가 Y축 범위를 스크롤/확대/축소할 수 없도록 고정
        ),
        # 전체 차트 레이아웃의 마진(여백)을 조정하여 플로팅 영역을 최적화합니다.
        margin=dict(l=50, r=50, t=0, b=0), # 좌, 우, 상단, 하단 마진 설정
    )

    return fig, top_level_tasks # 생성된 차트 객체와 최상위 프로젝트 목록을 반환합니다.

# --- 6. Streamlit 앱 실행 로직 ---
if __name__ == "__main__":
    # Notion API 토큰과 데이터베이스 ID가 유효하게 설정되었는지 확인합니다.
    if not notion_token or not db_id:
        st.error("Streamlit Secrets(`NOTION_TOKEN`, `DATABASE_ID`)이 설정되지 않았습니다.")
        st.info("`.streamlit/secrets.toml` 파일에 Notion API 토큰과 데이터베이스 ID를 추가하거나, Streamlit Community Cloud에서 Secrets를 설정해주세요.")
    else:
        # Notion 데이터베이스에서 원본 데이터를 가져옵니다.
        raw_notion_data = get_notion_database_data(db_id)

        if raw_notion_data: # Notion 데이터가 성공적으로 로드된 경우
            # 가져온 원본 데이터를 Pandas DataFrame으로 가공합니다.
            df_processed = process_notion_data(raw_notion_data)
            
            if not df_processed.empty: # 가공된 DataFrame에 데이터가 있는 경우
                # 앱 상단에 프로젝트 일정 요약 제목을 스타일과 함께 표시합니다.
                st.markdown(
                    """
                    <div style="background-color:#FFA500; color:white; padding:10px; border-radius:5px; text-align:center; font-size:24px; margin-bottom: 20px;">
                        <b>프로젝트 일정 Summary</b>
                    </div>
                    """,
                    unsafe_allow_html=True
                )
                
                # 타임라인 차트를 생성하고 최상위 프로젝트 목록을 가져옵니다.
                chart_figure, top_level_tasks = create_timeline_chart(df_processed) 
                
                num_categories = len(top_level_tasks) # 최상위 프로젝트(카테고리)의 수를 계산합니다.
                
                # Streamlit 차트의 높이를 동적으로 계산합니다.
                # 'y_axis_map'에서 사용된 간격 계수를 고려하여 각 라인(프로젝트)당 필요한 실제 높이를 산정합니다.
                # 이 값을 조절하여 전체 차트의 높이를 조정할 수 있습니다.
                height_per_actual_category = 50 # 각 라벨에 할당할 픽셀 높이 (조절 가능)

                min_chart_height = 250 # 차트의 최소 높이를 설정합니다.
                
                # 동적 높이를 계산합니다: 전체 카테고리 수와 각 카테고리당 필요한 높이를 곱합니다.
                # 'y_axis_spacing_factor'가 높을수록 차트의 세로 공간을 더 많이 차지합니다.
                dynamic_height = max(min_chart_height, int(num_categories * height_per_actual_category * (chart_figure.layout.yaxis.range[0] - chart_figure.layout.yaxis.range[1]) / len(top_level_tasks)))

                # Plotly 차트를 Streamlit 앱에 표시합니다. 컨테이너 너비에 맞추고 동적으로 계산된 높이를 적용합니다.
                st.plotly_chart(chart_figure, use_container_width=True, height=dynamic_height)
            else:
                # 가공된 데이터가 없는 경우 경고 메시지를 표시합니다.
                st.warning("표시할 프로젝트 데이터가 없습니다. Notion 데이터베이스를 확인해주세요.")
        else:
            # Notion 데이터 로드에 실패한 경우 정보 메시지를 표시합니다.
            st.info("Notion 데이터베이스에서 데이터를 가져오지 못했습니다. API 설정 또는 네트워크 연결을 확인해주세요.")