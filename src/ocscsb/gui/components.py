import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog

from ocscsb.library.processing import Processor

class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root

        root.title("CSB Processing Pipeline")
        root.geometry("975x750")

        # --- Main Frame ---
        main_frame = ttk.Frame(root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # --- Variable Definitions ---
        self.csb_var = tk.StringVar()
        self.BAG_filepath_var = tk.StringVar()
        self.fp_zones_var = tk.StringVar()
        self.output_dir_var = tk.StringVar()
        self.tessellation_shp_var = tk.StringVar()
        grid_resolution_var = tk.StringVar(value="10")
        export_final_gpkg_var = tk.BooleanVar(value=True)
        self.fes_model_var = tk.BooleanVar(value=False)
        self.fes_path_var = tk.StringVar()
        self.fes_yaml_var = tk.StringVar()

        # BooleanVars for options
        self.bluetopo_var = tk.BooleanVar()
        self.duckdb_option_var = tk.BooleanVar(value=True)
        self.export_gp_var = tk.BooleanVar(value=False)
        self.run_analysis_var = tk.BooleanVar(value=True)
        export_transits_var = tk.BooleanVar(value=False)
        self.run_final_grid_var = tk.BooleanVar(value=True)
        organize_vrt_var = tk.BooleanVar(value=True)

        # --- Input Files Section ---
        input_frame = ttk.LabelFrame(main_frame, text="1. Input Files & Folders", padding="10")
        input_frame.pack(fill=tk.X, expand=True, pady=5)

        ttk.Label(input_frame, text='Raw CSB CSV Directory').grid(row=0, column=0, sticky='w', padx=5, pady=2)
        csb_dir_entry = ttk.Entry(input_frame, textvariable=self.csb_var, width=60)
        csb_dir_entry.grid(row=0, column=1)
        ttk.Button(input_frame, text='Browse', command=lambda: MainWindow.open_folder_dialog(self.csb_var)).grid(row=0, column=2,
                                                                                                 padx=5)

        ttk.Label(input_frame, text='Tide Zone Shapefile').grid(row=1, column=0, sticky='w', padx=5, pady=2)
        fp_zones_entry = ttk.Entry(input_frame, textvariable=self.fp_zones_var, width=60)
        fp_zones_entry.grid(row=1, column=1)
        ttk.Button(input_frame, text='Browse',
                   command=lambda: MainWindow.open_file_dialog(self.fp_zones_var, [("Shapefile", "*.shp")])).grid(row=1, column=2,
                                                                                                  padx=5)

        ttk.Label(input_frame, text='Output Directory').grid(row=2, column=0, sticky='w', padx=5, pady=2)
        output_dir_entry = ttk.Entry(input_frame, textvariable=self.output_dir_var, width=60)
        output_dir_entry.grid(row=2, column=1)
        ttk.Button(input_frame, text='Browse',
                   command=lambda: MainWindow.open_folder_dialog(self.output_dir_var)).grid(row=2, column=2, padx=5)

        # --- Reference Bathymetry Section ---
        ref_bathy_frame = ttk.LabelFrame(main_frame, text="2. Reference Bathymetry", padding="10")
        ref_bathy_frame.pack(fill=tk.X, expand=True, pady=5)

        bluetopo_checkbox = ttk.Checkbutton(ref_bathy_frame, text="Use Automated BlueTopo Download",
                                            variable=self.bluetopo_var)
        bluetopo_checkbox.grid(row=0, column=0, columnspan=3, sticky='w', padx=5, pady=2)

        ttk.Label(ref_bathy_frame, text='Local BAG/GeoTiff File').grid(row=1, column=0, sticky='w', padx=5, pady=2)
        self.BAG_filepath_entry = ttk.Entry(ref_bathy_frame, textvariable=self.BAG_filepath_var, width=60)
        self.BAG_filepath_entry.grid(row=1, column=1)
        ttk.Button(ref_bathy_frame, text='Browse',
                   command=lambda: MainWindow.open_file_dialog(self.BAG_filepath_var,
                                                               [("BAG or GeoTIFF file",
                                                                 "*.bag;*.tif;*.tiff")])).grid(row=1, column=2, padx=5)

        # --- Processing Options Section ---
        options_frame = ttk.LabelFrame(main_frame, text="3. Processing & Export Options", padding="10")
        options_frame.pack(fill=tk.X, expand=True, pady=5)

        row_num = 0

        ttk.Checkbutton(options_frame, text="Insert into DuckDB (Required for all post-processing)",
                        variable=self.duckdb_option_var).grid(row=row_num, column=0, columnspan=2, sticky='w', padx=5)
        row_num += 1

        ttk.Checkbutton(options_frame, text="Export Initial Processed Geopackage (per input file)",
                        variable=self.export_gp_var).grid(row=row_num, column=0, columnspan=2, sticky='w', padx=5)
        row_num += 1

        ttk.Separator(options_frame, orient='horizontal').grid(row=row_num, columnspan=3, sticky='ew', pady=5)
        row_num += 1

        # Post-Processing Options
        ttk.Checkbutton(options_frame, text="Run Post-Processing (Outlier Flagging, etc.)",
                        variable=self.run_analysis_var).grid(row=row_num, column=0, columnspan=2, sticky='w', padx=5)
        row_num += 1

        self.export_transits_checkbox = ttk.Checkbutton(options_frame,
                                                   text="Export Individual Transit Files (GPKG & GeoTIFF)",
                                                   variable=export_transits_var)
        self.export_transits_checkbox.grid(row=row_num, column=0, sticky='w', padx=25)
        row_num += 1

        # --- FES Model Section (Our new widgets) ---
        ttk.Separator(options_frame, orient='horizontal').grid(row=row_num, columnspan=3, sticky='ew', pady=5)
        row_num += 1

        fes_checkbox = ttk.Checkbutton(options_frame,
                                       text="Use FES Tide Model (for non-US waters) - user must download AVISO FES2022b ocean_tide model from AVISO website",
                                       variable=self.fes_model_var)
        fes_checkbox.grid(row=row_num, column=0, columnspan=2, sticky='w', padx=5)
        row_num += 1

        ttk.Label(options_frame, text='FES Model Data Path').grid(row=row_num, column=0, sticky='w', padx=25)
        fes_path_entry = ttk.Entry(options_frame, textvariable=self.fes_path_var, width=45)
        fes_path_entry.grid(row=row_num, column=1, sticky='w')
        fes_path_button = ttk.Button(options_frame, text='Browse', command=lambda: MainWindow.open_folder_dialog(self.fes_path_var))
        fes_path_button.grid(row=row_num, column=2, padx=5)
        row_num += 1

        ttk.Label(options_frame, text='FES Config YAML File').grid(row=row_num, column=0, sticky='w', padx=25)
        fes_yaml_entry = ttk.Entry(options_frame, textvariable=self.fes_yaml_var, width=45)
        fes_yaml_entry.grid(row=row_num, column=1, sticky='w')
        fes_yaml_button = ttk.Button(options_frame, text='Browse',
                                     command=lambda: MainWindow.open_file_dialog(self.fes_yaml_var, [("YAML file", "*.yml")]))
        fes_yaml_button.grid(row=row_num, column=2, padx=5)
        row_num += 1

        ttk.Separator(options_frame, orient='horizontal').grid(row=row_num, columnspan=3, sticky='ew', pady=5)
        row_num += 1

        # Final Gridding Options
        final_grid_checkbox = ttk.Checkbutton(options_frame, text="Run Final Gridding & Export",
                                              variable=self.run_final_grid_var)
        final_grid_checkbox.grid(row=row_num, column=0, sticky='w', padx=5)
        row_num += 1

        gpkg_export_checkbox = ttk.Checkbutton(options_frame, text="Export Final Points GeoPackage (in epsg:4326)",
                                               variable=export_final_gpkg_var)
        gpkg_export_checkbox.grid(row=row_num, column=0, columnspan=2, sticky='w', padx=25)
        row_num += 1

        ttk.Label(options_frame, text='Optional Tessellation Shapefile').grid(row=row_num, column=0, sticky='w',
                                                                              padx=25)
        self.tess_entry = ttk.Entry(options_frame, textvariable=self.tessellation_shp_var, width=45)
        self.tess_entry.grid(row=row_num, column=1, sticky='w')
        self.tess_button = ttk.Button(options_frame, text='Browse',
                                      command=lambda: MainWindow.open_file_dialog(self.tessellation_shp_var, [("Shapefile", "*.shp")]))
        self.tess_button.grid(row=row_num, column=2, padx=5)
        row_num += 1

        ttk.Label(options_frame, text='Grid Resolution (meters)').grid(row=row_num, column=0, sticky='w', padx=25)
        self.res_entry = ttk.Entry(options_frame, textvariable=grid_resolution_var, width=10)
        self.res_entry.grid(row=row_num, column=1, sticky='w')
        row_num += 1

        self.organize_vrt_checkbox = ttk.Checkbutton(options_frame, text="Organize GeoTIFFs by EPSG and Create VRTs",
                                                variable=organize_vrt_var)
        self.organize_vrt_checkbox.grid(row=row_num, column=0, columnspan=2, sticky='w', padx=25)
        row_num += 1
        # --- Process Button ---
        process_button = ttk.Button(main_frame, text='Start Processing', command=self.process_csb_threaded)
        process_button.pack(pady=15)

    # --- GUI Logic ---
    def on_bluetopo_check(self):
        if self.bluetopo_var.get():
            self.BAG_filepath_entry.config(state='disabled')
            self.BAG_filepath_var.set("")
        else:
            self.BAG_filepath_entry.config(state='normal')

    def toggle_analysis_options(self):
        state = 'normal' if self.run_analysis_var.get() else 'disabled'
        self.export_transits_checkbox.config(state=state)

    def toggle_final_grid_options(self):
        state = 'normal' if self.run_final_grid_var.get() else 'disabled'
        self.tess_entry.config(state=state)
        self.tess_button.config(state=state)
        self.res_entry.config(state=state)
        self.organize_vrt_checkbox.config(state=state)
        if state == 'disabled':
            self.tessellation_shp_var.set("")

    def process_csb_threaded(self):
        csb_directory = self.csb_var.get()
        fp_zones = self.fp_zones_var.get()
        bag_file_path = self.BAG_filepath_var.get()
        output_dir = self.output_dir_var.get()
        use_bluetopo = self.bluetopo_var.get()
        use_fes_model = self.fes_model_var.get()
        fes_data_path = self.fes_path_var.get()
        fes_yaml_path = self.fes_yaml_var.get()
        run_analysis = self.run_analysis_var.get()
        run_final_grid = self.run_final_grid_var.get()
        export_gp = self.export_gp_var.get()
        insert_duckdb = self.duckdb_option_var.get()
        if not os.path.isdir(csb_directory):
            print("Selected path is not a directory.")
            return
        processor: Processor = Processor(
            csb_directory,
            fp_zones,
            bag_file_path,
            output_dir,
            clean_up_callback=lambda: self.root.destroy(),
            use_bluetopo=use_bluetopo,
            use_fes_model=use_fes_model,
            fes_data_path=fes_data_path,
            fes_yaml_path=fes_yaml_path,
            run_analysis=run_analysis,
            run_final_grid=run_final_grid,
            export_gp=export_gp,
            insert_duckdb=insert_duckdb
        )
        processing_thread = threading.Thread(target=processor.run)
        processing_thread.start()

    @staticmethod
    def open_folder_dialog(var):
        foldername = filedialog.askdirectory()
        var.set(foldername)

    @staticmethod
    def open_file_dialog(var, file_types):
        filename = filedialog.askopenfilename(filetypes=file_types)
        var.set(filename)

# Entrypoint for running in an IDE without having to install the entire package
if __name__ == '__main__':
    root = tk.Tk()
    win = MainWindow(root)
    tk.mainloop()
