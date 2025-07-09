import os
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from notion_client import Client
from datetime import timedelta

# --- 1. 설정 및 초기화 ---
NOTION_TOKEN = "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"
DATABASE_ID = "22ade43458148015b875d2d090e06d1c"

st.set_page_config(layout="wide", page_title="프로젝트 마일스톤 타임라인")

notion = Client(auth=NOTION_TOKEN)

# --- 2. Notion 데이터 가져오기 ---
def get_notion_database_data(database_id: str) -> list:
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
            if not response["has_more"]:
                break
            start_cursor = response["next_cursor"]
        except Exception as e:
            st.error(f"Notion 데이터 로드 중 오류가 발생했습니다: {e}")
            return []
    return all_results

# --- 3. Notion 데이터 가공 ---
def process_notion_data(notion_pages: list) -> pd.DataFrame:
    processed_items = []
    for item in notion_pages:
        properties = item.get("properties", {})

        name_prop = properties.get("프로젝트 이름", {}).get("title", [])
        project_name = name_prop[0]["plain_text"] if name_prop else "이름 없음"

        if project_name == "이름 없음":
            continue

        end_date_obj = properties.get("종료일", {}).get("date")
        end_date = end_date_obj["start"] if end_date_obj and "start" in end_date_obj else None
        
        status_prop = properties.get("상태", {})
        status = status_prop.get("status", {}).get("name") if status_prop.get("type") == "status" else \
                 status_prop.get("select", {}).get("name") if status_prop.get("type") == "select" else "미정"

        parent_relation_prop = properties.get("상위 항목", {}).get("relation", [])
        parent_id = parent_relation_prop[0]["id"] if parent_relation_prop else None

        processed_items.append({
            "id": item["id"],
            "이름": project_name,
            "종료일": end_date,
            "상태": status,
            "상위 항목 ID": parent_id,
        })
    
    df = pd.DataFrame(processed_items)
    df["종료일"] = pd.to_datetime(df["종료일"], errors='coerce')
    
    return df

# --- 4. 하위 태스크 데이터 수집 ---
def get_descendant_end_details(task_id: str, df_all_tasks_indexed: pd.DataFrame, parent_child_map: dict) -> list:
    descendant_details = []
    
    if task_id in parent_child_map:
        for child_id in parent_child_map.get(task_id, []):
            try:
                child_task = df_all_tasks_indexed.loc[[child_id]]
            except KeyError:
                child_task = pd.DataFrame()

            if not child_task.empty and pd.notna(child_task["종료일"].iloc[0]):
                descendant_details.append({
                    'date': child_task["종료일"].iloc[0],
                    'name': child_task["이름"].iloc[0],
                    'status': child_task["상태"].iloc[0]
                })
            descendant_details.extend(get_descendant_end_details(child_id, df_all_tasks_indexed, parent_child_map))
            
    return descendant_details

# --- 5. 타임라인 차트 생성 ---
def create_timeline_chart(df: pd.DataFrame) -> go.Figure:
    top_level_tasks = df[df["상위 항목 ID"].isnull()].copy()
    top_level_tasks = top_level_tasks.sort_values(by="이름", ascending=True) 

    fig = go.Figure()

    parent_child_map = {}
    for _, row in df.iterrows():
        if pd.notna(row["상위 항목 ID"]) and row["상위 항목 ID"] in df['id'].values:
            parent_child_map.setdefault(row["상위 항목 ID"], []).append(row["id"])

    all_descendant_end_dates = []
    df_indexed_by_id = df.set_index('id') 

    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        # get_descendant_end_details 호출 시 df_indexed_by_id를 명시적으로 전달
        descendant_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        all_descendant_end_dates.extend([d['date'] for d in descendant_details])

    valid_end_dates = pd.Series(all_descendant_end_dates).dropna()

    min_date = valid_end_dates.min() if not valid_end_dates.empty else pd.Timestamp.now() - timedelta(days=30)
    max_date = valid_end_dates.max() if not valid_end_dates.empty else pd.Timestamp.now() + timedelta(days=30)
    
    fig.add_trace(go.Scatter(
        x=[min_date, max_date],
        y=top_level_tasks["이름"].tolist(),
        mode='text',
        showlegend=False,
        hoverinfo='skip'
    ))

    fig.update_yaxes(autorange="reversed") 

    plotly_qualitative_colors = px.colors.qualitative.Plotly 
    
    global_color_index = 0
    for _, top_task in top_level_tasks.iterrows():
        top_task_id = top_task["id"]
        top_task_name = top_task["이름"] # 최상위 프로젝트 이름

        # 현재 최상위 태스크에 속한 모든 하위 태스크의 종료일 상세 정보를 가져옵니다.
        descendant_end_details = get_descendant_end_details(top_task_id, df_indexed_by_id, parent_child_map)
        
        if descendant_end_details:
            descendant_end_details.sort(key=lambda x: x['date'])
            
            x_coords = [d['date'] for d in descendant_end_details]
            y_coords = [top_task_name] * len(x_coords)
            
            # --- 수정된 부분: 호버 텍스트에 프로젝트 이름 추가 ---
            hover_texts = [
                f"<b>{top_task_name}</b><br>"  # 최상위 프로젝트 이름 추가
                f"<b>{d['name']}</b><br>"       # 하위 태스크 이름
                f"{d['date'].strftime('%Y/%m/%d')}"
                for d in descendant_end_details
            ]
            # --- 수정 끝 ---

            colors_for_points = []
            for _ in descendant_end_details:
                colors_for_points.append(plotly_qualitative_colors[global_color_index % len(plotly_qualitative_colors)])
                global_color_index += 1 

            fig.add_trace(
                go.Scatter(
                    x=x_coords,
                    y=y_coords,
                    mode='lines+markers',
                    marker=dict(
                        symbol='circle',
                        size=15,
                        color=colors_for_points,
                        line=dict(width=1, color='DarkSlateGrey')
                    ),
                    line=dict(color='DarkSlateGrey', width=5),
                    name=f"{top_task_name} 하위 종료일",
                    hoverinfo='text',
                    hovertext=hover_texts,
                    showlegend=False
                )
            )
    
    fig.update_layout(
        title="", 
        xaxis_title=dict(
            text="날짜",
            font=dict(size=20)
        ),
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
        yaxis=dict(
            showgrid=True,
            tickfont=dict(size=16),
            automargin=True,
            ticklen=5,
        ),
    )

    return fig, top_level_tasks 

# --- 6. Streamlit 앱 실행 로직 ---
if __name__ == "__main__":
    if not NOTION_TOKEN or not DATABASE_ID:
        st.error("Notion API 토큰 또는 데이터베이스 ID가 설정되지 않았습니다.")
        st.info("`NOTION_TOKEN`과 `DATABASE_ID` 변수를 올바르게 설정해주세요.")
    else:
        raw_notion_data = get_notion_database_data(DATABASE_ID)

        if raw_notion_data:
            df_processed = process_notion_data(raw_notion_data)
            
            if not df_processed.empty:
                st.markdown(
                    """
                    <div style="background-color:#FFA500; color:white; padding:10px; border-radius:5px; text-align:center; font-size:24px; margin-bottom: 20px;">
                        <b>프로젝트 일정 Summary</b>
                    </div>
                    """,
                    unsafe_allow_html=True 
                )
                
                chart_figure, top_level_tasks = create_timeline_chart(df_processed) 
                
                num_categories = len(top_level_tasks) 
                
                height_per_category = 60 
                min_chart_height = 250 
                
                dynamic_height = max(min_chart_height, num_categories * height_per_category)

                st.plotly_chart(chart_figure, use_container_width=True, height=dynamic_height)
            else:
                st.warning("표시할 프로젝트 데이터가 없습니다. Notion 데이터베이스를 확인해주세요.")
        else:
            st.info("Notion 데이터베이스에서 데이터를 가져오지 못했습니다. API 설정 또는 네트워크 연결을 확인해주세요.")