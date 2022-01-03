from types import SimpleNamespace
from typing import Any, List, Set

from AnyQt.QtCore import Qt, Signal, QSortFilterProxyModel, QItemSelection, QItemSelectionModel
from AnyQt.QtWidgets import QLabel, QGridLayout, QFormLayout, QLineEdit, \
    QTableView, QListView, QTreeWidget, QTreeWidgetItem
from AnyQt.QtGui import QStandardItem

from Orange.data import Table
from Orange.data.pandas_compat import table_from_frame
from Orange.widgets.settings import Setting, ContextSetting
from Orange.widgets.utils.concurrent import ConcurrentWidgetMixin, TaskState
from Orange.widgets.utils.itemmodels import PyTableModel, TableModel
from Orange.widgets.widget import OWWidget, Output
from Orange.widgets import gui
import time
from orangecontrib.owwhstudy.whstudy import WorldIndicators, AggregationMethods

MONGO_HANDLE = WorldIndicators('main', 'biolab')


def run(
        countries: List,
        indicators: List,
        years: List,
        agg_method: int,
        index_freq: int,
        state: TaskState
) -> Table:
    if not countries or not indicators or not years:
        return None

    # Define progress callback
    def callback(i: float, status=""):
        state.set_progress_value(i * 100)
        if status:
            state.set_status(status)
        if state.is_interruption_requested():
            raise Exception

    indicator_codes = [code for (_, code, _) in indicators]

    print(indicator_codes)
    print(years)
    print(countries)

    main_df = MONGO_HANDLE.data(countries, indicator_codes, years, callback=callback, index_freq=index_freq)
    results = table_from_frame(main_df)

    # Add descriptions to indicators
    for attrib in results.domain.attributes:
        for (_, code, desc) in indicators:
            if code in attrib.name:
                attrib.attributes["description"] = desc

    results = AggregationMethods.aggregate(results, indicators, years, agg_method if len(years) > 1 else 0)
    return results


class CountryTreeWidgetItem(QTreeWidgetItem):
    def __init__(self, parent, key, code):
        super().__init__(parent, [key])
        self.country_code = code


class IndexTableView(QTableView):
    pressedAny = Signal()

    def __init__(self):
        super().__init__(
            sortingEnabled=True,
            editTriggers=QTableView.NoEditTriggers,
            selectionBehavior=QTableView.SelectRows,
            selectionMode=QTableView.ExtendedSelection,
            cornerButtonEnabled=False,
        )
        self.setItemDelegate(gui.ColoredBarItemDelegate(self))
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setDefaultSectionSize(22)
        self.verticalHeader().hide()

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.pressedAny.emit()


class IndicatorTableItem(QStandardItem):
    def __init__(self, text):
        super().__init__(text)


class IndicatorTableModel(PyTableModel):
    def wrap(self, table):
        table = [(db, code, desc) for (code, desc, db, _) in table]
        super().wrap(table)

    def data(self, index, role=Qt.DisplayRole):
        if role == Qt.BackgroundColorRole and index.column() == 0:
            return TableModel.ColorForRole[TableModel.Meta]
        return super().data(index, role)


class IndicatorFilterProxyModel(QSortFilterProxyModel):
    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder):
        super().sort(column, order)


class OWWHStudy(OWWidget, ConcurrentWidgetMixin):
    name = "Socioeconomic Indices"
    description = "Gets requested socioeconomic data from WDB/WHR."
    icon = "icons/mywidget.svg"
    want_main_area = True
    resizing_enabled = True

    agg_method: int = Setting(AggregationMethods.NONE)
    indicator_freq: float = Setting(60)
    selected_years: List = Setting([])
    selected_indicators: List = Setting([])
    selected_countries: Set = Setting(set())
    auto_apply: bool = Setting(False)

    class Outputs:
        world_data = Output("World data", Table)

    def __init__(self):
        OWWidget.__init__(self)
        ConcurrentWidgetMixin.__init__(self)
        super().__init__()
        self.world_data = None
        self.year_features = []
        self.country_features = MONGO_HANDLE.countries()
        self.indicator_features = MONGO_HANDLE.indicators()
        self.indicator_model = IndicatorTableModel(parent=self)
        self._setup_gui()

        # Assign values to control views
        self.year_features = [str(y) for y in range(2020, 1960, -1)]
        self.indicator_model.wrap(self.indicator_features)
        self.indicator_model.setHorizontalHeaderLabels(['Source', 'Index', 'Relative', 'Description'])
        self.indicator_view.resizeColumnToContents(0)
        self.indicator_view.resizeColumnToContents(1)
        self.indicator_view.resizeRowsToContents()
        ctree = self.create_country_tree(self.country_features)
        self.country_tree.itemChanged.connect(self.country_checked)
        self.set_country_tree(ctree, self.country_tree)

    def _setup_gui(self):
        fbox = gui.widgetBox(self.controlArea, "", orientation=0)

        box = gui.widgetBox(fbox, "Indicator Filtering")
        vbox = gui.hBox(box)

        grid = QFormLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        vbox.layout().addLayout(grid)
        spin = gui.spin(vbox, self, 'indicator_freq', minv=1, maxv=100)
        grid.addRow("Index frequency (%)", spin)

        vbox = gui.vBox(box)
        gui.listBox(
            vbox, self, 'selected_years', labels='year_features',
            selectionMode=QListView.ExtendedSelection, box='Years'
        )

        abox = gui.vBox(box, "Aggregation by year")
        gui.comboBox(
            abox, self, "agg_method", items=AggregationMethods.ITEMS
        )
        bbox = gui.vBox(box)
        gui.auto_send(bbox, self, "auto_apply")

        box = gui.widgetBox(fbox, "Countries")
        self.__country_filter_line_edit = QLineEdit(
            textChanged=self.__on_country_filter_changed,
            placeholderText="Filter..."
        )

        self.country_tree = QTreeWidget()
        self.country_tree.setFixedWidth(400)
        self.country_tree.setColumnCount(1)
        self.country_tree.setColumnWidth(0, 300)
        self.country_tree.setHeaderLabels(['Countries'])
        box.layout().addWidget(self.country_tree)

        box = gui.widgetBox(self.mainArea, "Indicator Selection")
        self.__indicator_filter_line_edit = QLineEdit(
            textChanged=self.__on_indicator_filter_changed,
            placeholderText="Filter..."
        )
        box.layout().addWidget(self.__indicator_filter_line_edit)

        self.indicator_view = IndexTableView()
        self.indicator_view.horizontalHeader().sectionClicked.connect(
            self.__on_indicator_horizontal_header_clicked)
        box.layout().addWidget(self.indicator_view)

        proxy = IndicatorFilterProxyModel()
        proxy.setFilterKeyColumn(-1)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.indicator_view.setModel(proxy)
        self.indicator_view.setSortingEnabled(True)
        self.indicator_view.model().setSourceModel(self.indicator_model)
        self.indicator_view.selectionModel().selectionChanged.connect(
            self.__on_indicator_selection_changed
        )

    def __on_country_filter_changed(self):
        pass

    def __on_indicator_filter_changed(self):
        model = self.indicator_view.model()
        model.setFilterFixedString(self.__indicator_filter_line_edit.text().strip())
        self._select_indicator_rows()

    def __on_indicator_selection_changed(self):
        selected_rows = self.indicator_view.selectionModel().selectedRows(1)
        model = self.indicator_view.model()
        self.selected_indicators = [
            (model.data(model.index(i.row(), 0)),
             model.data(model.index(i.row(), 1)),
             model.data(model.index(i.row(), 2)))
            for i in selected_rows]
        self.commit()

    def __on_indicator_horizontal_header_clicked(self):
        pass

    def on_exception(self, ex: Exception):
        raise ex

    def on_done(self, result: Any):
        self.Outputs.world_data.send(result)

    def on_partial_result(self, result: Any) -> None:
        pass

    def commit(self):
        years = []
        for i in self.selected_years:
            years.append(int(self.year_features[i]))
        self.start(
            run, list(self.selected_countries), self.selected_indicators,
            years, self.agg_method, self.indicator_freq
        )

    def country_checked(self, item: CountryTreeWidgetItem, column):
        if item.country_code is not None:
            if item.checkState(column) == Qt.Checked:
                self.selected_countries.add(item.country_code)
            else:
                self.selected_countries.discard(item.country_code)

    def _clear(self):
        self.clear_messages()
        self.cancel()
        self.selected_countries = set()
        self.selected_indicators = []
        self.selected_years = []

    @staticmethod
    def create_country_tree(data):
        regions = [('AFR', 'Africa'), ('ECS', 'Europe & Central Asia'),
                   ('EAS', 'East Asia & Pacific'), ('LCN', 'Latin America and the Caribbean'),
                   ('NAC', 'North America'), ('SAS', 'South Asia')]
        members = [
            {'RWA', 'TZA', 'DZA', 'MAR', 'SEN', 'BDI', 'MOZ', 'GIN', 'EGY', 'MUS', 'GNQ', 'CIV', 'ZAF', 'SLE', 'STP',
             'UGA', 'ZWE', 'GNB', 'AGO', 'MDG', 'CPV', 'TCD', 'COD', 'COM', 'MWI', 'LSO', 'NGA', 'COG', 'NER', 'BFA',
             'SYC', 'SSD', 'TGO', 'ETH', 'TUN', 'SOM', 'KEN', 'DJI', 'BWA', 'LBY', 'ERI', 'GHA', 'GAB', 'GMB', 'CMR',
             'MRT', 'SDN', 'SWZ', 'BEN', 'NAM', 'MLI', 'LBR', 'CAF', 'ZMB'},
            {'AUT', 'HRV', 'SWE', 'TJK', 'AND', 'UKR', 'TUR', 'NOR', 'BIH', 'FIN', 'FRO', 'CYP', 'GBR', 'HUN', 'ISL',
             'BEL', 'PRT', 'MCO', 'IMN', 'LTU', 'MDA', 'SVK', 'CHI', 'TKM', 'LVA', 'SRB', 'MKD', 'FRA', 'LIE', 'ESP',
             'GRL', 'GIB', 'ITA', 'SMR', 'IRL', 'DNK', 'POL', 'AZE', 'CHE', 'EST', 'ARM', 'KAZ', 'LUX', 'ALB', 'GEO',
             'NLD', 'DEU', 'SVN', 'CZE', 'MNE', 'RUS', 'BLR', 'GRC', 'XKX', 'UZB', 'KGZ', 'ROU', 'BGR'},
            {'JPN', 'PRK', 'VNM', 'THA', 'FSM', 'HKG', 'MMR', 'SGP', 'TUV', 'TON', 'PNG', 'VUT', 'NRU', 'ASM', 'PYF',
             'MYS', 'SLB', 'AUS', 'FJI', 'BRN', 'MNG', 'PHL', 'PLW', 'TWN', 'KHM', 'KOR', 'KIR', 'MHL', 'LAO', 'GUM',
             'MNP', 'IDN', 'WSM', 'MAC', 'NCL', 'NZL', 'TLS', 'CHN'},
            {'GRD', 'BRB', 'SUR', 'VEN', 'DOM', 'BOL', 'GTM', 'LCA', 'JAM', 'VCT', 'HTI', 'PER', 'SXM', 'TCA', 'GUY',
             'MAF', 'ECU', 'BHS', 'MEX', 'ATG', 'HND', 'VIR', 'KNA', 'DMA', 'BLZ', 'PRI', 'NIC', 'COL', 'CYM', 'URY',
             'VGB', 'CHL', 'PAN', 'BRA', 'TTO', 'ABW', 'CUB', 'ARG', 'SLV', 'CUW', 'PRY', 'CRI'}, {'USA', 'CAN', 'BMU'},
            {'LKA', 'MDV', 'IND', 'AFG', 'NPL', 'BGD', 'BTN', 'PAK'}]
        tree = {'All': {
            'Regions': {
                'Africa': [],
                'Europe & Central Asia': [],
                'East Asia & Pacific': [],
                'Latin America and the Caribbean': [],
                'North America': [],
                'South Asia': []
            }
        }}
        regions_node = tree['All']['Regions']
        for (code, name) in data:
            for ind in range(len(regions)):
                if code in members[ind]:
                    regions_node[regions[ind][1]].append((code, name))
        return tree

    def set_country_tree(self, data, parent):
        for key in sorted(data):
            node = CountryTreeWidgetItem(parent, key[1], key[0]) if isinstance(key, tuple) else \
                CountryTreeWidgetItem(parent, key, None)
            state = Qt.Checked if key in self.selected_countries else Qt.Unchecked
            node.setCheckState(0, state)
            if isinstance(data, dict) and data[key]:
                node.setExpanded(key == 'All' or key == 'Regions')
                node.setFlags(node.flags() | Qt.ItemIsAutoTristate)
                self.set_country_tree(data[key], node)

    def _select_indicator_rows(self):
        model = self.indicator_view.model()
        n_rows, n_columns = model.rowCount(), model.columnCount()
        selection = QItemSelection()
        for i in range(n_rows):
            indicator = model.data(model.index(i, 1))
            if indicator in self.selected_indicators:
                _selection = QItemSelection(model.index(i, 0),
                                            model.index(i, n_columns - 1))
                selection.merge(_selection, QItemSelectionModel.Select)

        self.indicator_view.selectionModel().select(
            selection, QItemSelectionModel.ClearAndSelect
        )


if __name__ == "__main__":
    from Orange.widgets.utils.widgetpreview import WidgetPreview  # since Orange 3.20.0

    WidgetPreview(OWWHStudy).run()
