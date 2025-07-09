import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from notion_client import Client
from datetime import timedelta

# --- 1. 설정 및 초기화 ---
# Notion API 인증 토큰과 데이터베이스 ID를 Streamlit Secrets에서 가져옵니다.
notion_token = st.secrets["NOTION_TOKEN"]
db_id = st.secrets["DATABASE_ID"]

# Streamlit 앱 페이지의 기본 설정을 지정합니다.
st.set_page_config(layout="wide", page_title="프로젝트 마일스톤 타임라인")

# Notion 클라이언트 인스턴스를 초기화합니다.
notion = Client(auth=notion_token)

# --- 2. Notion 데이터 가져오기 ---
@st.cache_data(ttl=600) # 10분마다 데이터를 새로고침하여 API 호출 횟수를 최적화합니다.
def get_notion_database_data(database_id: str) -> list:
    """
    지정된 Notion 데이터베이스에서 모든 페이지(항목) 데이터를 가져옵니다.
    데이터는 '프로젝트 이름' 속성을 기준으로 오름차순으로 정렬됩니다.
    API 호출 중 오류가 발생하면 빈 리스트를 반환합니다.
    """
    all_results = []
    start_cursor = None

    while True:
        try:
            response = notion.databases.query(
                database_id=database_id,
                start_cursor=start_cursor,
                sorts=[
                    {"property": "프로젝트 이름", "direction": "ascending"}
                ]
            )
            all_results.extend(response["results"])
            # 더 이상 가져올 데이터가 없으면 반복을 중단합니다.
            if not response["has_more"]:
                break
            start_cursor = response["next_cursor"]
        except Exception as e:
            st.error(f"Notion 데이터 로드 중 오류가 발생했습니다: {e}")
            return [] # 오류 발생 시 빈 리스트 반환
    return all_results

# --- 3. Notion 데이터 가공 ---
def process_notion_data(notion_pages: list) -> pd.DataFrame:
    """
    가져온 Notion 페이지 데이터를 Pandas DataFrame으로 가공합니다.
    - '프로젝트 이름', '종료일', '상태', '상위 항목 ID' 정보를 추출합니다.
    - '프로젝트 이름'이 "이름 없음"인 항목은 제외합니다.
    - '종료일'은 datetime 객체로 변환하며, 유효하지 않은 날짜는 NaT(Not a Time)로 처리합니다.
    """
    processed_items = []
    for item in notion_pages:
        properties = item.get("properties", {})

        # '프로젝트 이름' 속성 추출
        name_prop = properties.get("프로젝트 이름", {}).get("title", [])
        project_name = name_prop[0]["plain_text"] if name_prop else "이름 없음"

        # '이름 없음'으로 지정된 항목은 시각화에서 제외합니다.
        if project_name == "이름 없음":
            continue

        # '종료일' 속성 추출 (Notion Date 속성의 'start' 필드 사용)
        end_date_obj = properties.get("종료일", {}).get("date")
        end_date = end_date_obj["start"] if end_date_obj and "start" in end_date_obj else None
        
        # '상태' 속성 추출 (Status 또는 Select 타입 모두 지원)
        status_prop = properties.get("상태", {})
        status = status_prop.get("status", {}).get("name") if status_prop.get("type") == "status" else \
                 status_prop.get("select", {}).get("name") if status_prop.get("type") == "select" else "미정"

        # '상위 항목' 관계 속성 추출
        parent_relation_prop = properties.get("상위 항목", {}).get("relation", [])
        parent_id = parent_relation_prop[0]["id"] if parent_relation_prop else None

        processed_items.append({
            "id": item["id"],
            "이름": project_name,
            "종료일": end_date,
            "상태": status,
            "상위 항목 ID": parent_id,
        })
    
    # 리스트를 DataFrame으로 변환합니다.
    df = pd.DataFrame(processed_items)
    
    # '종료일' 컬럼을 datetime 형식으로 변환합니다. 오류 발생 시 NaT로 처리됩니다.
    df["종료일"] = pd.to_datetime(df["종료일"], errors='coerce')
    
    return df

# --- 4. 하위 태스크 데이터 수집 ---
def get_descendant_end_details(task_id: str, df_all_tasks_indexed: pd.DataFrame, parent_child_map: dict) -> list:
    """
    주어진 `task_id`에 해당하는 상위 태스크의 모든 하위(자식, 손자 등) 태스크의 종료일과 세부 정보를 재귀적으로 수집합니다.
    주의: 최상위 태스크 자체의 종료일은 이 함수에 포함되지 않습니다.
    `df_all_tasks_indexed`는 'id'를 인덱스로 설정한 DataFrame이어야 합니다.
    """
    descendant_details = []
    
    # 현재 태스크 ID가 부모-자식 맵에 존재하면 하위 태스크를 탐색합니다.
    if task_id in parent_child_map:
        for child_id in parent_child_map.get(task_id, []):
            try:
                # 'id'를 인덱스로 하는 DataFrame에서 해당 자식 태스크의 행을 직접 선택합니다.
                child_task = df_all_tasks_indexed.loc[[child_id]]
            except KeyError:
                # 해당 child_id가 DataFrame 인덱스에 없는 경우 (예: "이름 없음"으로 스킵된 항목)
                child_task = pd.DataFrame() # 빈 DataFrame으로 처리하여 다음 조건에서 건너뜁니다.

            # 자식 태스크가 존재하고 종료일이 유효한 경우 세부 정보를 추가합니다.
            if not child_task.empty and pd.notna(child_task["종료일"].iloc[0]):
                descendant_details.append({
                    'date': child_task["종료일"].iloc[0],
                    'name': child_task["이름"].iloc[0],
                    'status': child_task["상태"].iloc[0]
                })
            # 재귀적으로 현재 자식 태스크의 하위 태스크를 탐색하여 결과에 추가합니다.
            descendant_details.extend(get_descendant_end_details(child_id, df_all_tasks_indexed, parent_child_map))
            
    return descendant_details

# --- 5. 타임라인 차트 생성 ---
def create_timeline_chart(df: pd.DataFrame) -> go.Figure:
    """
    가공된 Pandas DataFrame을 사용하여 Plotly 점 연결 타임라인 차트를 생성합니다.
    - Y축에는 최상위 프로젝트 이름이 표시됩니다.
    - 각 프로젝트의 하위 태스크 종료일이 점으로 표시되고, 점들은 선으로 연결됩니다.
    - X축(날짜) 범위는 모든 하위 태스크의 종료일을 기반으로 자동 설정됩니다.
    - X축 및 Y축 제목, Y축 라벨의 폰트 크기를 조정합니다.
    - **하위 항목의 이름이 같으면 같은 색으로 표시됩니다.**
    """
    # 상위 항목 ID가 없는 항목들을 최상위 태스크로 간주하여 Y축 라벨로 사용합니다.
    top_level_tasks = df[df["상위 항목 ID"].isnull()].copy()
    
    # Y축 라벨의 순서를 위해 최상위 태스크들을 이름 기준으로 정렬합니다.
    top_level_tasks = top_level_tasks.sort_values(by="이름", ascending=True) 

    # Plotly Figure 객체를 초기화합니다.
    fig = go.Figure()

    # 재귀 탐색을 위한 부모 -> 자식 ID 맵을 생성합니다.
    parent_child_map = {}
    for _, row in df.iterrows():
        if pd.notna(row["상위 항목 ID"]) and row["상위 항목 ID"] in df['id'].values:
            parent_child_map.setdefault(row["상위 항목 ID"], []).append(row["id"])

    all_descendant_end_dates = []
    df_indexed_by_id = df.set_index('id') 

    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        descendant_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        all_descendant_end_dates.extend([d['date'] for d in descendant_details])

    # 유효한(NaT가 아닌) 종료일만 필터링합니다.
    valid_end_dates = pd.Series(all_descendant_end_dates).dropna()

    # X축(날짜) 범위의 최소값과 최대값을 설정합니다.
    # 유효한 종료일이 없으면 현재 날짜를 기준으로 기본 범위를 설정합니다.
    min_date = valid_end_dates.min() if not valid_end_dates.empty else pd.Timestamp.now() - timedelta(days=30)
    max_date = valid_end_dates.max() if not valid_end_dates.empty else pd.Timestamp.now() + timedelta(days=30)
    
    # Y축 카테고리를 설정하기 위한 더미 Scatter 트레이스를 추가합니다.
    fig.add_trace(go.Scatter(
        x=[min_date, max_date], # X축 범위의 기준점을 제공
        y=top_level_tasks["이름"].tolist(), # 최상위 항목 이름을 Y축 카테고리로 사용
        mode='text', # 텍스트 모드 (실제 텍스트는 표시하지 않음)
        showlegend=False, # 범례에 표시하지 않음
        hoverinfo='skip' # 호버 정보도 표시하지 않음
    ))

    # Y축 순서를 뒤집어 최신 프로젝트가 상단에 오도록 합니다.
    fig.update_yaxes(autorange="reversed") 

    # Plotly의 기본 색상 팔레트를 가져옵니다.
    plotly_qualitative_colors = px.colors.qualitative.Plotly 
    
    # --- 수정된 부분 시작: 하위 항목 이름에 따른 색상 매핑 ---
    # 모든 고유한 하위 항목 이름을 추출합니다.
    all_descendant_names = sorted(list(set(d['name'] for top_task in top_level_tasks.iterrows() 
                                         for d in get_descendant_end_details(top_task[1]["id"], df_indexed_by_id, parent_child_map))))

    # 하위 항목 이름에 색상을 매핑하는 딕셔너리를 생성합니다.
    # 고유한 하위 항목 이름의 개수가 Plotly 팔레트의 색상 개수를 초과할 수 있으므로,
    # 팔레트의 색상을 반복하여 사용합니다.
    color_map = {}
    for i, name in enumerate(all_descendant_names):
        color_map[name] = plotly_qualitative_colors[i % len(plotly_qualitative_colors)]
    # --- 수정된 부분 끝 ---

    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        top_task_name = top_task["이름"] # 최상위 프로젝트 이름

        # 현재 최상위 태스크에 속한 모든 하위 태스크의 종료일 상세 정보를 가져옵니다.
        descendant_end_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        
        if descendant_end_details:
            # 점들을 날짜 순으로 정렬하여 선이 올바르게 연결되도록 합니다.
            descendant_end_details.sort(key=lambda x: x['date'])
            
            x_coords = [d['date'] for d in descendant_end_details]
            y_coords = [top_task_name] * len(x_coords)
            
            # 각 점에 대한 호버 텍스트를 구성합니다 (프로젝트 이름, 태스크 이름, 날짜).
            hover_texts = [
                f"<b>프로젝트: {top_task_name}</b><br>"
                f"<b>태스크: {d['name']}</b><br>"
                f"날짜: {d['date'].strftime('%Y/%m/%d')}"
                for d in descendant_end_details
            ]

            # --- 수정된 부분: 하위 항목 이름에 따라 색상 할당 ---
            colors_for_points = [color_map[d['name']] for d in descendant_end_details]
            # --- 수정 끝 ---
            
            # Scatter 트레이스를 추가하여 점과 선을 그립니다.
            fig.add_trace(
                go.Scatter(
                    x=x_coords,
                    y=y_coords,
                    mode='lines+markers', # 선과 마커(점) 모두 표시
                    marker=dict(
                        symbol='circle',
                        size=15,
                        color=colors_for_points, # 각 점에 개별 색상 적용
                        line=dict(width=1, color='DarkSlateGrey') # 점 테두리 색상
                    ),
                    line=dict(color='DarkSlateGrey', width=5), # 선 색상 및 두께 설정
                    name=f"{top_task_name} 하위 종료일", # 이 트레이스의 이름 (범례에는 표시 안됨)
                    hoverinfo='text', # 호버 시 `hovertext`만 표시
                    hovertext=hover_texts,
                    showlegend=False # 범례에 표시하지 않음
                )
            )
    
    # 차트의 전체 레이아웃을 설정합니다.
    fig.update_layout(
        # Plotly 차트 제목은 Streamlit 컴포넌트를 사용하므로 여기서는 빈 값으로 설정합니다.
        title="", 
        # X축 제목 및 폰트 설정
        xaxis_title=dict(
            text="날짜",
            font=dict(size=20) # X축 제목 폰트 크기 20으로 설정
        ),
        # Y축 제목 및 폰트 설정
        yaxis_title=dict(
            text="프로젝트",
            font=dict(size=20) # Y축 제목 폰트 크기 20으로 설정
        ),
        hovermode="closest", # 마우스와 가장 가까운 점의 정보만 표시
        xaxis=dict(
            autorange=True, # X축 범위 자동 설정
            showgrid=True, # X축 그리드 라인 표시
            tickformat="%Y/%m/%d", # X축 날짜 틱 포맷
            tickfont=dict(size=14) # X축 라벨(날짜) 폰트 크기 조정 (필요시 조절)
        ), 
        # Y축 라벨 폰트 크기 조정 및 자동 마진 설정
        yaxis=dict(
            showgrid=True,
            tickfont=dict(size=16), # Y축 라벨 폰트 크기를 16으로 조정
            automargin=True, # Y축 라벨이 잘리지 않도록 자동 마진 조정
            ticklen=5 # Y축 틱 마크 길이를 줄여 라벨 공간 확보 (기본값은 5)
        ),
    )

    return fig, top_level_tasks 

# --- 6. Streamlit 앱 실행 로직 ---
if __name__ == "__main__":
    # Notion API 토큰 또는 데이터베이스 ID가 설정되었는지 확인합니다.
    # Streamlit Secrets를 사용하므로, secrets.toml 파일이 없으면 에러가 발생할 수 있습니다.
    if not notion_token or not db_id:
        st.error("Streamlit Secrets(`NOTION_TOKEN`, `DATABASE_ID`)이 설정되지 않았습니다.")
        st.info("`.streamlit/secrets.toml` 파일에 Notion API 토큰과 데이터베이스 ID를 추가하거나, Streamlit Community Cloud에서 Secrets를 설정해주세요.")
    else:
        # Notion 데이터베이스에서 데이터를 가져옵니다.
        raw_notion_data = get_notion_database_data(db_id)

        # Notion 데이터 로드에 성공했을 경우
        if raw_notion_data:
            # 가져온 원본 데이터를 가공하여 DataFrame으로 만듭니다.
            df_processed = process_notion_data(raw_notion_data)
            
            # 가공된 DataFrame이 비어있지 않다면 차트를 생성하고 표시합니다.
            if not df_processed.empty:
                # '프로젝트 일정 Summary' 제목을 Streamlit Markdown으로 표시합니다.
                st.markdown(
                    """
                    <div style="background-color:#FFA500; color:white; padding:10px; border-radius:5px; text-align:center; font-size:24px; margin-bottom: 20px;">
                        <b>프로젝트 일정 Summary</b>
                    </div>
                    """,
                    unsafe_allow_html=True # HTML 태그 사용을 허용합니다.
                )
                
                # 타임라인 차트를 생성하고, 최상위 태스크 목록도 함께 받습니다.
                chart_figure, top_level_tasks = create_timeline_chart(df_processed) 
                
                # Y축 라벨 개수에 따라 동적으로 높이 계산
                num_categories = len(top_level_tasks) # Y축에 표시될 프로젝트 개수
                
                # 각 카테고리 라벨이 차지할 높이 (픽셀 단위, 조절 가능)
                height_per_category = 60 # 이 값을 조정하여 Y축 라벨 간격을 조절합니다.
                
                # 차트의 최소 기본 높이 (제목, 축 제목, X축 라벨, 여백 등 고정 공간)
                min_chart_height = 250 
                
                # 최종 차트 높이 계산
                dynamic_height = max(min_chart_height, num_categories * height_per_category)

                # Streamlit에 Plotly 차트를 표시합니다. 컨테이너 너비에 맞춰 조정됩니다.
                st.plotly_chart(chart_figure, use_container_width=True, height=dynamic_height)
            else:
                # Notion 데이터는 가져왔지만, 가공 후 표시할 데이터가 없는 경우 (예: 모든 항목이 "이름 없음")
                st.warning("표시할 프로젝트 데이터가 없습니다. Notion 데이터베이스를 확인해주세요.")
        else:
            # Notion 데이터 로드 자체가 실패한 경우 (get_notion_database_data에서 이미 오류 메시지 출력)
            st.info("Notion 데이터베이스에서 데이터를 가져오지 못했습니다. API 설정 또는 네트워크 연결을 확인해주세요.")